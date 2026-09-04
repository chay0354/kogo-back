"""B2C website checkout → CRM iframe initiate / webhook failure handling."""
import json
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Branch
from apps.core.payment_service import PaymentService, parse_store_cart_notes
from apps.store.stock_utils import decrement_product_stock
from apps.store.models import StoreInvoice, StoreProduct, StoreProductSize, StoreSale


@override_settings(
    WEBSITE_INTEGRATION_API_KEY='test-key',
    WEBSITE_INTEGRATION_URL='',
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_PROD_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_PUBLIC_KEY='prod_pk',
    TRANZILA_PROD_SECRET_KEY='prod_sk',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)
class WebsitePaymentInitiateTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {'HTTP_X_INTEGRATION_KEY': 'test-key'}
        self.product = StoreProduct.objects.create(
            name='חולצה',
            category='ביגוד',
            sale_price=Decimal('4.00'),
            cost_price=Decimal('1.00'),
            stock_quantity=10,
            website_legacy_id=4399,
            is_active=True,
        )

    def _payload(self, order_number='CG-260824-WQQ5', **extra):
        body = {
            'website_order_number': order_number,
            'idempotency_key': extra.pop('idempotency_key', f'idemp-{order_number}'),
            'callback_url': 'https://crm.example/api/v1/store/payment/callback/',
            'success_url': 'https://shop.example/checkout/complete?order=' + order_number,
            'error_url': 'https://shop.example/checkout/complete?order=' + order_number,
            'customer': {'name': 'דור סער', 'email': 'doreden8@gmail.com', 'phone': '0500000000'},
            'items': [{'legacy_id': 4399, 'quantity': 1}],
        }
        body.update(extra)
        return body

    def test_initiate_uses_iframe_terminal_not_production_rest_terminal(self):
        res = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(),
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 201, res.content)
        url = res.json()['iframe_url']
        self.assertIn('/iframe_terminal/iframenew.php', url)
        self.assertNotIn('prod_rest_terminal', url)

    def test_failed_invoice_with_cart_notes_can_be_retried(self):
        first = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(),
            format='json',
            **self.headers,
        )
        self.assertEqual(first.status_code, 201, first.content)
        invoice = StoreInvoice.objects.get(website_order_number='CG-260824-WQQ5')
        cart = invoice.notes
        invoice.payment_status = 'failed'
        invoice.save(update_fields=['payment_status'])

        retry = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(),
            format='json',
            **self.headers,
        )
        self.assertEqual(retry.status_code, 200, retry.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'pending')
        self.assertEqual(invoice.notes, cart)
        self.assertIn('/iframe_terminal/iframenew.php', retry.json()['iframe_url'])

    def test_failed_invoice_without_cart_notes_requires_new_order(self):
        invoice = StoreInvoice.objects.create(
            customer_name='דור סער',
            customer_phone='0500000000',
            customer_email='doreden8@gmail.com',
            total_amount=Decimal('4.00'),
            payment_method='credit_card',
            payment_status='failed',
            website_order_number='CG-260824-BROKEN',
            website_idempotency_key='idemp-broken',
            notes='Payment failed: קוד שגיאה 141',
        )
        res = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(order_number='CG-260824-BROKEN', idempotency_key='idemp-broken'),
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 400)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')

    def test_shipping_charged_once_for_three_products(self):
        self.product.delivery_price = Decimal('15.00')
        self.product.sale_price = Decimal('100.00')
        self.product.save()
        extras = []
        for i, legacy in enumerate((4400, 4401), start=1):
            extras.append(StoreProduct.objects.create(
                name=f'פריט {i}',
                category='ביגוד',
                sale_price=Decimal('100.00'),
                cost_price=Decimal('1.00'),
                delivery_price=Decimal('15.00'),
                stock_quantity=10,
                website_legacy_id=legacy,
                is_active=True,
            ))
        res = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(
                order_number='CG-260826-SHIP1',
                items=[
                    {'legacy_id': 4399, 'quantity': 1},
                    {'legacy_id': 4400, 'quantity': 1},
                    {'legacy_id': 4401, 'quantity': 2},
                ],
            ),
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 201, res.content)
        invoice = StoreInvoice.objects.get(website_order_number='CG-260826-SHIP1')
        # 100+100+200 products + 15 shipping once (not 15×4 units or 15×3 lines)
        self.assertEqual(invoice.total_amount, Decimal('415.00'))
        cart = parse_store_cart_notes(invoice.notes)
        self.assertIsNotNone(cart)
        self.assertTrue(all(row.get('line_delivery') is False for row in cart))
        self.assertTrue(all(row.get('branch') == 'delivery' for row in cart))


@override_settings(
    WEBSITE_INTEGRATION_API_KEY='test-key',
    WEBSITE_INTEGRATION_URL='',
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_PROD_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_PUBLIC_KEY='prod_pk',
    TRANZILA_PROD_SECRET_KEY='prod_sk',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)
class WebsitePickupPaymentTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {'HTTP_X_INTEGRATION_KEY': 'test-key'}
        self.branch = Branch.objects.create(name='אם המושבות, רפאל איתן 5', is_active=True)
        self.product = StoreProduct.objects.create(
            name='חולצה',
            category='ביגוד',
            sale_price=Decimal('90.00'),
            cost_price=Decimal('10.00'),
            delivery_price=Decimal('20.00'),
            stock_quantity=0,
            website_legacy_id=4500,
            is_active=True,
        )
        self.delivery_row = StoreProductSize.objects.create(
            product=self.product,
            size='M',
            stock_quantity=5,
            sort_order=0,
            branch=None,
        )
        self.pickup_row = StoreProductSize.objects.create(
            product=self.product,
            size='M',
            stock_quantity=2,
            sort_order=1,
            branch=self.branch,
        )
        self.product.recalculate_total_stock()

    def test_a_product_without_sizes_can_still_hold_stock_per_location(self):
        """
        Most of the catalog has no sizes, and until now that meant one number
        for every location — "pickup" drew on the delivery pool. A product can
        now keep a row per place with no size at all, and the two are separate.
        """
        flat = StoreProduct.objects.create(
            name='ספר', category='מתנות', sale_price=Decimal('50.00'),
            cost_price=Decimal('10.00'), delivery_price=Decimal('20.00'),
            stock_quantity=0, website_legacy_id=4700, is_active=True,
        )
        StoreProductSize.objects.create(
            product=flat, size='', stock_quantity=6, sort_order=0, branch=None,
        )
        branch_row = StoreProductSize.objects.create(
            product=flat, size='', stock_quantity=1, sort_order=1, branch=self.branch,
        )
        flat.recalculate_total_stock()

        delivery = self.client.post(
            '/api/v1/store/widget/stock-check/',
            {'items': [{'legacy_id': 4700, 'quantity': 6}], 'delivery_method': 'delivery'},
            format='json',
            **self.headers,
        )
        self.assertEqual(delivery.json()['items'][0]['available'], 6)

        pickup = self.client.post(
            '/api/v1/store/widget/stock-check/',
            {'items': [{'legacy_id': 4700, 'quantity': 2}], 'delivery_method': 'pickup'},
            format='json',
            **self.headers,
        )
        self.assertFalse(pickup.json()['items'][0]['ok'])
        self.assertEqual(pickup.json()['items'][0]['available'], 1)

        # And a pickup sale comes off the branch row, not the delivery one.
        decrement_product_stock(flat, {'quantity': 1, 'branch': str(self.branch.id)})
        branch_row.refresh_from_db()
        self.assertEqual(branch_row.stock_quantity, 0)
        flat.refresh_from_db()
        self.assertEqual(flat.stock_quantity, 6)

    def test_a_stock_row_may_carry_a_location_without_a_size(self):
        """The write path has to keep such a row — it used to drop it silently."""
        from apps.store.serializers import _normalize_size_stocks

        rows = _normalize_size_stocks([
            {'size': '', 'stock_quantity': 4, 'branch': None},
            {'size': '', 'stock_quantity': 2, 'branch': str(self.branch.id)},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['stock_quantity'] for r in rows}, {4, 2})
        self.assertEqual({r['branch'] for r in rows}, {None, str(self.branch.id)})

    def test_a_product_with_no_size_rows_draws_on_one_pool_either_way(self):
        """
        The branch split lives in the size rows. A product that carries a single
        total has nowhere else to draw from, so pickup and delivery read the
        same number — worth pinning, because it looks like a branch was ignored.
        """
        flat = StoreProduct.objects.create(
            name='בובה', category='מתנות', sale_price=Decimal('50.00'),
            cost_price=Decimal('10.00'), delivery_price=Decimal('20.00'),
            stock_quantity=3, website_legacy_id=4600, is_active=True,
        )
        self.assertFalse(flat.has_per_size_stock())

        for method in ('delivery', 'pickup'):
            res = self.client.post(
                '/api/v1/store/widget/stock-check/',
                {'items': [{'legacy_id': 4600, 'quantity': 3}], 'delivery_method': method},
                format='json',
                **self.headers,
            )
            self.assertEqual(res.status_code, 200, res.content)
            self.assertEqual(res.json()['items'][0]['available'], 3, method)

    def test_pickup_is_refused_when_the_branch_has_no_row_for_that_size(self):
        """Stock sitting only on the delivery row is not available for pickup."""
        self.pickup_row.delete()
        self.product.recalculate_total_stock()

        res = self.client.post(
            '/api/v1/store/widget/stock-check/',
            {'items': [{'legacy_id': 4500, 'quantity': 1, 'variant': 'M'}], 'delivery_method': 'pickup'},
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()['items'][0]['ok'])
        self.assertEqual(res.json()['items'][0]['available'], 0)

    def test_stock_check_uses_pickup_branch_not_delivery(self):
        delivery = self.client.post(
            '/api/v1/store/widget/stock-check/',
            {'items': [{'legacy_id': 4500, 'quantity': 3, 'variant': 'M'}], 'delivery_method': 'delivery'},
            format='json',
            **self.headers,
        )
        self.assertEqual(delivery.status_code, 200)
        self.assertTrue(delivery.json()['items'][0]['ok'])
        self.assertEqual(delivery.json()['items'][0]['available'], 5)

        pickup_too_many = self.client.post(
            '/api/v1/store/widget/stock-check/',
            {'items': [{'legacy_id': 4500, 'quantity': 3, 'variant': 'M'}], 'delivery_method': 'pickup'},
            format='json',
            **self.headers,
        )
        self.assertEqual(pickup_too_many.status_code, 200)
        self.assertFalse(pickup_too_many.json()['items'][0]['ok'])
        self.assertEqual(pickup_too_many.json()['items'][0]['available'], 2)

        pickup_ok = self.client.post(
            '/api/v1/store/widget/stock-check/',
            {'items': [{'legacy_id': 4500, 'quantity': 2, 'variant': 'M'}], 'delivery_method': 'pickup'},
            format='json',
            **self.headers,
        )
        self.assertTrue(pickup_ok.json()['items'][0]['ok'])

    def test_pickup_initiate_skips_shipping_and_records_branch_stock(self):
        res = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            {
                'website_order_number': 'CG-260826-PICK',
                'idempotency_key': 'idemp-pick',
                'callback_url': 'https://crm.example/api/v1/store/payment/callback/',
                'success_url': 'https://shop.example/ok',
                'error_url': 'https://shop.example/err',
                'customer': {'name': 'דור', 'email': 'a@b.co', 'phone': '0500000000'},
                'items': [{'legacy_id': 4500, 'quantity': 1, 'variant': 'M'}],
                'delivery_method': 'pickup',
                'pickup_branch_id': str(self.branch.id),
            },
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 201, res.content)
        invoice = StoreInvoice.objects.get(website_order_number='CG-260826-PICK')
        self.assertEqual(invoice.total_amount, Decimal('90.00'))
        self.assertEqual(str(invoice.branch_id), str(self.branch.id))
        cart = parse_store_cart_notes(invoice.notes)
        self.assertEqual(len(cart), 1)
        self.assertEqual(cart[0]['branch'], str(self.branch.id))
        self.assertEqual(cart[0]['size_stock_id'], str(self.pickup_row.id))
        self.assertFalse(cart[0]['line_delivery'])

        PaymentService().complete_store_purchase_from_webhook(
            invoice_id=str(invoice.id),
            tranzila_response={
                'is_successful': True,
                'response_code': '000',
                'error_message': '',
                'transaction_id': 'txn-1',
                'confirmation_code': 'ok',
            },
        )
        self.delivery_row.refresh_from_db()
        self.pickup_row.refresh_from_db()
        self.assertEqual(self.delivery_row.stock_quantity, 5)
        self.assertEqual(self.pickup_row.stock_quantity, 1)
        sale = StoreSale.objects.get(invoice=invoice)
        self.assertEqual(str(sale.branch_id), str(self.branch.id))
        self.assertEqual(sale.unit_price, Decimal('90.00'))


@override_settings(WEBSITE_INTEGRATION_URL='', WEBSITE_INTEGRATION_API_KEY='')
class StoreWebhookFailurePreservesCartTest(TestCase):
    def test_failed_webhook_does_not_overwrite_cart_json(self):
        cart = [{'product_id': '11111111-1111-1111-1111-111111111111', 'quantity': 1}]
        invoice = StoreInvoice.objects.create(
            customer_name='דור סער',
            total_amount=Decimal('4.00'),
            payment_method='credit_card',
            payment_status='pending',
            website_order_number='CG-260824-WQQ5',
            notes=json.dumps(cart),
        )
        result = PaymentService().complete_store_purchase_from_webhook(
            invoice_id=str(invoice.id),
            tranzila_response={
                'is_successful': False,
                'response_code': '141',
                'error_message': 'המסוף אינו מורשה לסלוק את סוג הכרטיס',
                'transaction_id': '',
                'confirmation_code': '',
            },
        )
        self.assertFalse(result['success'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertEqual(parse_store_cart_notes(invoice.notes), cart)
