"""
Editing a product in the CRM store changes only what the office changed.

The edit form sends the product's stock rows as it loaded them and as the
office left them. Rows are updated in place (their ids are what the till's
cart, website orders and the write-off history point at), a row the office did
not touch keeps whatever a sale left in it meanwhile, and a row it did touch is
saved only if nothing moved it since the form opened.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.store.models import InventoryAdjustment, StoreInvoice, StoreProduct, StoreProductSize, StoreSale
from apps.store.stock_utils import available_stock_for_item, decrement_product_stock, restore_stock_for_sale

User = get_user_model()


class _Store(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='store-manager@test', password='pw-for-tests')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.user = User.objects.get(pk=user.pk)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.branch = Branch.objects.create(name='סניף מרכז', is_active=True)
        self.other = Branch.objects.create(name='סניף צפון', is_active=True)
        self.shirt = StoreProduct.objects.create(
            name='חולצה', category='ביגוד', sale_price=Decimal('90'), cost_price=Decimal('40'), stock_quantity=0,
        )
        self.m_delivery = StoreProductSize.objects.create(product=self.shirt, size='M', branch=None, stock_quantity=6, sort_order=0)
        self.m_branch = StoreProductSize.objects.create(product=self.shirt, size='M', branch=self.branch, stock_quantity=3, sort_order=1)
        self.shirt.recalculate_total_stock()

    def rows(self, product=None):
        product = product or self.shirt
        return {
            (r.size, r.branch_id): r.stock_quantity
            for r in StoreProductSize.objects.filter(product=product)
        }

    def form_rows(self, product=None):
        """The rows as the edit form loads them (from the list)."""
        product = product or self.shirt
        return [
            {'size': r.size, 'stock_quantity': r.stock_quantity, 'sort_order': r.sort_order,
             'branch': str(r.branch_id) if r.branch_id else None}
            for r in StoreProductSize.objects.filter(product=product).order_by('sort_order')
        ]

    def patch(self, product, body):
        return self.client.patch(f'/api/v1/store/products/{product.id}/', body, format='json')


class RowsKeepTheirIdsTests(_Store):
    def test_an_edit_updates_rows_in_place(self):
        adjustment = InventoryAdjustment.objects.create(
            product=self.shirt, size_stock=self.m_delivery, quantity_delta=-1, reason='damage',
        )
        loaded = self.form_rows()
        saved = [dict(r) for r in loaded]
        saved[0]['stock_quantity'] = 8
        res = self.patch(self.shirt, {'name': 'חולצה כחולה', 'size_stocks': saved,
                                      'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 200, res.content)
        ids = set(StoreProductSize.objects.filter(product=self.shirt).values_list('id', flat=True))
        self.assertEqual(ids, {self.m_delivery.id, self.m_branch.id})
        self.m_delivery.refresh_from_db()
        self.assertEqual(self.m_delivery.stock_quantity, 8)
        # The write-off history still knows which row it was.
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.size_stock_id, self.m_delivery.id)
        self.shirt.refresh_from_db()
        self.assertEqual(self.shirt.stock_quantity, 11)

    def test_a_cart_line_built_before_the_edit_still_finds_its_stock(self):
        line = {'size_stock_id': str(self.m_branch.id), 'quantity': 1}
        loaded = self.form_rows()
        self.patch(self.shirt, {'sale_price': '95.00', 'size_stocks': loaded, 'stock_expected': {'rows': loaded}})
        self.assertEqual(available_stock_for_item(self.shirt, line), 3)


class OnlyWhatTheOfficeChangedTests(_Store):
    def test_a_sale_since_the_form_opened_is_not_undone(self):
        loaded = self.form_rows()
        decrement_product_stock(self.shirt, {'size_stock_id': str(self.m_delivery.id), 'quantity': 2})
        # The office only changes the price; the form still says 6.
        res = self.patch(self.shirt, {'sale_price': '99.00', 'size_stocks': loaded, 'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows()[('M', None)], 4)

    def test_changing_a_row_a_sale_moved_is_refused_with_a_reason(self):
        loaded = self.form_rows()
        decrement_product_stock(self.shirt, {'size_stock_id': str(self.m_delivery.id), 'quantity': 2})
        saved = [dict(r) for r in loaded]
        saved[0]['stock_quantity'] = 10
        res = self.patch(self.shirt, {'name': 'שם חדש', 'size_stocks': saved, 'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn('המלאי השתנה', str(res.data))
        self.assertIn('M · משלוח', str(res.data))
        # Nothing of the save went in.
        self.shirt.refresh_from_db()
        self.assertEqual(self.shirt.name, 'חולצה')
        self.assertEqual(self.rows()[('M', None)], 4)

    def test_changing_another_row_goes_through(self):
        loaded = self.form_rows()
        decrement_product_stock(self.shirt, {'size_stock_id': str(self.m_delivery.id), 'quantity': 2})
        saved = [dict(r) for r in loaded]
        saved[1]['stock_quantity'] = 5
        res = self.patch(self.shirt, {'size_stocks': saved, 'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows(), {('M', None): 4, ('M', self.branch.id): 5})

    def test_a_row_someone_else_added_meanwhile_is_kept(self):
        loaded = self.form_rows()
        added = StoreProductSize.objects.create(product=self.shirt, size='L', branch=None, stock_quantity=2)
        res = self.patch(self.shirt, {'size_stocks': loaded, 'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(StoreProductSize.objects.filter(pk=added.pk).exists())

    def test_removing_a_row_that_moved_meanwhile_is_refused(self):
        loaded = self.form_rows()
        decrement_product_stock(self.shirt, {'size_stock_id': str(self.m_branch.id), 'quantity': 1})
        res = self.patch(self.shirt, {'size_stocks': loaded[:1], 'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 409, res.content)
        self.assertTrue(StoreProductSize.objects.filter(pk=self.m_branch.pk).exists())

    def test_every_change_is_in_the_stock_history(self):
        loaded = self.form_rows()
        saved = [dict(loaded[0], stock_quantity=4), {'size': 'L', 'stock_quantity': 2, 'sort_order': 2, 'branch': None}]
        res = self.patch(self.shirt, {'size_stocks': saved, 'stock_expected': {'rows': loaded}})
        self.assertEqual(res.status_code, 200, res.content)
        history = {
            (a.note.split('— ')[-1], a.quantity_delta)
            for a in InventoryAdjustment.objects.filter(product=self.shirt, reason='recount')
        }
        self.assertEqual(history, {('M · משלוח', -2), ('M · סניף מרכז', -3), ('L · משלוח', 2)})
        self.assertTrue(all(a.adjusted_by_id == self.user.id for a in InventoryAdjustment.objects.all()))

    def test_an_older_client_without_what_it_loaded_still_saves_absolutely(self):
        saved = [dict(r) for r in self.form_rows()]
        saved[0]['stock_quantity'] = 1
        res = self.patch(self.shirt, {'size_stocks': saved})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows()[('M', None)], 1)
        self.assertEqual(StoreProductSize.objects.get(product=self.shirt, size='M', branch=None).pk, self.m_delivery.pk)


class StockPerLocationWithoutSizesTests(_Store):
    def setUp(self):
        super().setUp()
        self.bottle = StoreProduct.objects.create(
            name='בקבוק', category='אביזרים', sale_price=Decimal('25'), cost_price=Decimal('8'), stock_quantity=4,
        )

    def test_rows_without_a_size_save_through_the_staff_api(self):
        rows = [
            {'size': '', 'stock_quantity': 7, 'branch': str(self.branch.id)},
            {'size': '', 'stock_quantity': 12, 'branch': None},
        ]
        res = self.patch(self.bottle, {'size_stocks': rows, 'stock_expected': {'rows': [], 'stock_quantity': 4}})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows(self.bottle), {('', self.branch.id): 7, ('', None): 12})
        self.bottle.refresh_from_db()
        self.assertEqual(self.bottle.stock_quantity, 19)

    def test_creating_one_with_rows_without_a_size(self):
        res = self.client.post('/api/v1/store/products/', {
            'name': 'מגבת', 'sale_price': '30.00', 'cost_price': '10.00', 'stock_quantity': 0,
            'size_stocks': [{'size': '', 'stock_quantity': 3, 'branch': str(self.branch.id)}],
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['stock_quantity'], 3)

    def test_a_refund_goes_back_to_the_row_the_sale_came_from(self):
        row = StoreProductSize.objects.create(product=self.bottle, size='', branch=self.branch, stock_quantity=5)
        self.bottle.recalculate_total_stock()
        decrement_product_stock(self.bottle, {'size_stock_id': str(row.id), 'quantity': 2})
        invoice = StoreInvoice.objects.create(total_amount=Decimal('50'), payment_method='cash')
        sale = StoreSale.objects.create(
            invoice=invoice, product=self.bottle, quantity=2, unit_price=Decimal('25'), total_price=Decimal('50'),
            size='', branch=self.branch, payment_method='cash',
        )
        restore_stock_for_sale(sale)
        row.refresh_from_db()
        self.assertEqual(row.stock_quantity, 5)
        self.bottle.refresh_from_db()
        self.assertEqual(self.bottle.stock_quantity, 5)


class FlatStockTests(_Store):
    def setUp(self):
        super().setUp()
        self.bag = StoreProduct.objects.create(
            name='תיק', category='אביזרים', sale_price=Decimal('80'), cost_price=Decimal('30'), stock_quantity=10,
        )

    def test_an_unchanged_number_keeps_what_a_sale_left(self):
        decrement_product_stock(self.bag, {'quantity': 3})
        res = self.patch(self.bag, {'notes': 'x', 'stock_quantity': 10, 'size_stocks': [],
                                    'stock_expected': {'rows': [], 'stock_quantity': 10}})
        self.assertEqual(res.status_code, 200, res.content)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.stock_quantity, 7)

    def test_a_changed_number_needs_nothing_to_have_moved_it(self):
        decrement_product_stock(self.bag, {'quantity': 3})
        res = self.patch(self.bag, {'stock_quantity': 15, 'size_stocks': [],
                                    'stock_expected': {'rows': [], 'stock_quantity': 10}})
        self.assertEqual(res.status_code, 409, res.content)
        res = self.patch(self.bag, {'stock_quantity': 15, 'size_stocks': [],
                                    'stock_expected': {'rows': [], 'stock_quantity': 7}})
        self.assertEqual(res.status_code, 200, res.content)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.stock_quantity, 15)
        self.assertEqual(
            list(InventoryAdjustment.objects.filter(product=self.bag).values_list('quantity_delta', 'reason')),
            [(8, 'recount')],
        )


class AdjustAndTransferTests(_Store):
    def url(self, action, product=None):
        return f'/api/v1/store/products/{(product or self.shirt).id}/{action}/'

    def test_errors_are_in_hebrew_and_a_bad_id_is_not_a_500(self):
        cases = [
            ({'quantity_delta': -1, 'reason': 'damage', 'size_stock_id': 'not-a-uuid'}, 'לא תקינה'),
            ({'quantity_delta': -1, 'reason': 'damage', 'size_stock_id': '00000000-0000-0000-0000-000000000000'}, 'לא נמצאה'),
            ({'quantity_delta': -1, 'reason': 'damage'}, 'בחרו מידה ומיקום'),
            ({'quantity_delta': -1, 'reason': 'nope', 'size_stock_id': str(self.m_delivery.id)}, 'סיבה'),
            ({'quantity_delta': 0, 'reason': 'damage', 'size_stock_id': str(self.m_delivery.id)}, 'גדולה מ-0'),
        ]
        for body, words in cases:
            res = self.client.post(self.url('adjust_stock'), body, format='json')
            self.assertEqual(res.status_code, 400, body)
            self.assertIn(words, res.data['error'], body)

    def test_a_write_off_on_a_row(self):
        res = self.client.post(self.url('adjust_stock'), {
            'quantity_delta': -2, 'reason': 'damage', 'note': 'נקרע', 'size_stock_id': str(self.m_delivery.id),
        }, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows()[('M', None)], 4)
        adj = InventoryAdjustment.objects.get(product=self.shirt)
        self.assertEqual((adj.quantity_delta, adj.reason, adj.size_stock_id), (-2, 'damage', self.m_delivery.id))

    def test_a_product_without_rows_is_adjusted_on_its_number(self):
        bag = StoreProduct.objects.create(name='תיק', sale_price=Decimal('80'), cost_price=Decimal('30'), stock_quantity=10)
        res = self.client.post(self.url('adjust_stock', bag), {'quantity_delta': 5, 'reason': 'receipt'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        bag.refresh_from_db()
        self.assertEqual(bag.stock_quantity, 15)

    def test_transfer_errors_are_in_hebrew(self):
        res = self.client.post(self.url('transfer_stock'), {
            'quantity': 1, 'from_size_stock_id': str(self.m_delivery.id), 'to_size_stock_id': 'bad',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('לא תקינה', res.data['error'])
        res = self.client.post(self.url('transfer_stock'), {
            'quantity': 2, 'from_size_stock_id': str(self.m_delivery.id), 'to_size_stock_id': str(self.m_branch.id),
        }, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows(), {('M', None): 4, ('M', self.branch.id): 5})


class FirstRowsKeepTheStockTests(_Store):
    """A product that kept one number gets its first size: the number is not lost."""

    def setUp(self):
        super().setUp()
        self.bag = StoreProduct.objects.create(
            name='תיק', category='אביזרים', sale_price=Decimal('80'), cost_price=Decimal('30'),
            stock_quantity=10, branch=self.branch,
        )

    def test_the_form_carries_the_old_number_over_and_the_size_adds_to_it(self):
        rows = [
            {'size': '', 'stock_quantity': 10, 'branch': str(self.branch.id)},   # what the form carries over
            {'size': 'M', 'stock_quantity': 5, 'branch': str(self.branch.id)},
        ]
        res = self.patch(self.bag, {'size_stocks': rows, 'stock_quantity': 15,
                                    'stock_expected': {'rows': [], 'stock_quantity': 10}})
        self.assertEqual(res.status_code, 200, res.content)
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.stock_quantity, 15)
        self.assertEqual(self.rows(self.bag), {('', self.branch.id): 10, ('M', self.branch.id): 5})
        # One history line, for what really changed.
        self.assertEqual(
            list(InventoryAdjustment.objects.filter(product=self.bag).values_list('quantity_delta', 'note')),
            [(5, 'עדכון בחלון עריכת מוצר — מעבר למלאי לפי מידות ומיקומים')],
        )

    def test_a_sale_since_the_form_opened_refuses_the_move(self):
        decrement_product_stock(self.bag, {'quantity': 2})
        res = self.patch(self.bag, {'size_stocks': [{'size': 'M', 'stock_quantity': 5, 'branch': None}],
                                    'stock_expected': {'rows': [], 'stock_quantity': 10}})
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(StoreProductSize.objects.filter(product=self.bag).exists())

    def test_an_older_form_that_replaced_the_number_is_in_the_history(self):
        res = self.patch(self.bag, {'size_stocks': [{'size': 'M', 'stock_quantity': 5, 'branch': None}],
                                    'stock_quantity': 5})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(list(InventoryAdjustment.objects.filter(product=self.bag).values_list('quantity_delta', flat=True)), [-5])
