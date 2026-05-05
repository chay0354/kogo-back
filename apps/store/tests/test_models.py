"""
Unit tests for Store app models.

Tests coverage:
- StoreProduct: inventory, pricing, is_low_stock, profit_margin
- StoreInvoice: invoice generation, payment tracking
- StoreSale: line items, stock reduction
"""
from decimal import Decimal
from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.store.models import StoreProduct, StoreInvoice, StoreSale


class StoreProductModelTest(TestCase):
    """Test StoreProduct model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
    
    def test_create_store_product(self):
        """Test creating a store product"""
        product = StoreProduct.objects.create(
            name="חולצת קפואירה",
            category="ביגוד",
            size="S,M,L,XL",
            cost_price=Decimal('50.00'),
            sale_price=Decimal('100.00'),
            branch=self.branch,
            stock_quantity=20
        )
        
        self.assertEqual(product.name, "חולצת קפואירה")
        self.assertEqual(product.category, "ביגוד")
        self.assertEqual(product.sale_price, Decimal('100.00'))
        self.assertTrue(product.is_active)
    
    def test_store_product_is_low_stock_property(self):
        """Test store product is_low_stock property"""
        product = StoreProduct.objects.create(
            name="מוצר",
            sale_price=Decimal('100.00'),
            stock_quantity=2,
            min_stock_alert=3
        )
        
        self.assertTrue(product.is_low_stock)
        
        product.stock_quantity = 5
        product.save()
        
        self.assertFalse(product.is_low_stock)
    
    def test_store_product_profit_margin_property(self):
        """Test store product profit_margin calculation"""
        product = StoreProduct.objects.create(
            name="מוצר",
            cost_price=Decimal('50.00'),
            sale_price=Decimal('100.00')
        )
        
        # Profit margin = ((100 - 50) / 100) * 100 = 50%
        self.assertEqual(product.profit_margin, Decimal('50.00'))
    
    def test_store_product_profit_margin_zero_cost(self):
        """Test store product profit_margin when cost is zero"""
        product = StoreProduct.objects.create(
            name="מוצר",
            cost_price=Decimal('0.00'),
            sale_price=Decimal('100.00')
        )
        
        self.assertEqual(product.profit_margin, Decimal('0.00'))
    
    def test_store_product_with_sizes(self):
        """Test store product with multiple sizes"""
        product = StoreProduct.objects.create(
            name="מכנסיים",
            category="ביגוד",
            size="XS,S,M,L,XL,XXL",
            cost_price=Decimal('60.00'),
            sale_price=Decimal('120.00')
        )
        
        self.assertEqual(product.size, "XS,S,M,L,XL,XXL")
    
    def test_store_product_delivery_product(self):
        """Test store product for delivery (no branch)"""
        product = StoreProduct.objects.create(
            name="מוצר למשלוח",
            category="אביזרים",
            cost_price=Decimal('30.00'),
            sale_price=Decimal('60.00'),
            branch=None  # Delivery product
        )
        
        self.assertIsNone(product.branch)
    
    def test_store_product_with_image(self):
        """Test store product with image URL"""
        product = StoreProduct.objects.create(
            name="מוצר",
            cost_price=Decimal('50.00'),
            sale_price=Decimal('100.00'),
            image_url="https://example.com/product.jpg"
        )
        
        self.assertEqual(product.image_url, "https://example.com/product.jpg")
    
    def test_store_product_str_representation(self):
        """Test store product string representation"""
        product = StoreProduct.objects.create(
            name="חולצה",
            category="ביגוד",
            cost_price=Decimal('50.00'),
            sale_price=Decimal('100.00')
        )
        
        str_repr = str(product)
        self.assertIn("חולצה", str_repr)
        self.assertIn("ביגוד", str_repr)
    
    def test_store_product_is_active_flag(self):
        """Test store product can be deactivated"""
        product = StoreProduct.objects.create(
            name="מוצר",
            cost_price=Decimal('50.00'),
            sale_price=Decimal('100.00'),
            is_active=True
        )
        
        product.is_active = False
        product.save()
        product.refresh_from_db()
        
        self.assertFalse(product.is_active)
    
    def test_store_product_ordering(self):
        """Test store products are ordered by name"""
        StoreProduct.objects.create(
            name="זיפר",
            sale_price=Decimal('10.00')
        )
        
        StoreProduct.objects.create(
            name="אבזם",
            sale_price=Decimal('15.00')
        )
        
        StoreProduct.objects.create(
            name="נעליים",
            sale_price=Decimal('200.00')
        )
        
        products = list(StoreProduct.objects.all())
        self.assertEqual(products[0].name, "אבזם")
        self.assertEqual(products[1].name, "זיפר")
        self.assertEqual(products[2].name, "נעליים")


class StoreInvoiceModelTest(TestCase):
    """Test StoreInvoice model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.child = TestDataFactory.create_child()
    
    def test_create_store_invoice(self):
        """Test creating a store invoice"""
        invoice = StoreInvoice.objects.create(
            invoice_number="INV-TEST-001",
            child=self.child,
            total_amount=Decimal('150.00'),
            payment_method='credit_card',
            payment_status='completed',
            branch=self.branch
        )
        
        self.assertEqual(invoice.invoice_number, "INV-TEST-001")
        self.assertEqual(invoice.total_amount, Decimal('150.00'))
        self.assertEqual(invoice.payment_status, 'completed')
    
    def test_store_invoice_walk_in_customer(self):
        """Test store invoice for walk-in customer (no child)"""
        invoice = StoreInvoice.objects.create(
            invoice_number="INV-TEST-002",
            customer_name="יוסי כהן",
            customer_phone="050-1234567",
            total_amount=Decimal('80.00'),
            payment_method='cash',
            payment_status='completed'
        )
        
        self.assertIsNone(invoice.child)
        self.assertEqual(invoice.customer_name, "יוסי כהן")
        self.assertEqual(invoice.customer_phone, "050-1234567")
    
    def test_store_invoice_payment_method_choices(self):
        """Test store invoice payment method choices"""
        methods = ['credit_card', 'cash', 'monthly_billing']
        
        for idx, method in enumerate(methods):
            invoice = StoreInvoice.objects.create(
                invoice_number=f"INV-{idx}",
                total_amount=Decimal('100.00'),
                payment_method=method
            )
            self.assertEqual(invoice.payment_method, method)
    
    def test_store_invoice_payment_status_choices(self):
        """Test store invoice payment status choices"""
        statuses = ['pending', 'completed', 'failed', 'refunded', 'refund_failed']
        
        for idx, status in enumerate(statuses):
            invoice = StoreInvoice.objects.create(
                invoice_number=f"INV-STATUS-{idx}",
                total_amount=Decimal('100.00'),
                payment_method='cash',
                payment_status=status
            )
            self.assertEqual(invoice.payment_status, status)
    
    def test_store_invoice_with_refund(self):
        """Test store invoice with refunded amount"""
        invoice = StoreInvoice.objects.create(
            invoice_number="INV-REFUND-001",
            total_amount=Decimal('200.00'),
            refunded_amount=Decimal('50.00'),
            payment_method='credit_card',
            payment_status='refunded'
        )
        
        self.assertEqual(invoice.refunded_amount, Decimal('50.00'))
    
    def test_store_invoice_with_tranzila(self):
        """Test store invoice with Tranzila integration"""
        invoice = StoreInvoice.objects.create(
            invoice_number="INV-TRZ-001",
            total_amount=Decimal('150.00'),
            payment_method='credit_card',
            payment_status='completed',
            tranzila_confirmation_code="ABC123",
            charged_with_token=True
        )
        
        self.assertEqual(invoice.tranzila_confirmation_code, "ABC123")
        self.assertTrue(invoice.charged_with_token)
    
    def test_store_invoice_str_representation_with_child(self):
        """Test store invoice string representation with child"""
        invoice = StoreInvoice.objects.create(
            invoice_number="INV-STR-001",
            child=self.child,
            total_amount=Decimal('120.00'),
            payment_method='cash'
        )
        
        str_repr = str(invoice)
        self.assertIn("INV-STR-001", str_repr)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn('120', str_repr)
    
    def test_store_invoice_str_representation_walk_in(self):
        """Test store invoice string representation for walk-in"""
        invoice = StoreInvoice.objects.create(
            invoice_number="INV-STR-002",
            customer_name="לקוח אורח",
            total_amount=Decimal('80.00'),
            payment_method='cash'
        )
        
        str_repr = str(invoice)
        self.assertIn("לקוח אורח", str_repr)
    
    def test_store_invoice_auto_generate_invoice_number(self):
        """Test store invoice auto-generates invoice number"""
        invoice = StoreInvoice.objects.create(
            total_amount=Decimal('100.00'),
            payment_method='cash'
        )
        
        self.assertIsNotNone(invoice.invoice_number)
        self.assertTrue(invoice.invoice_number.startswith('INV-'))
    
    def test_store_invoice_unique_invoice_number(self):
        """Test store invoice number must be unique"""
        StoreInvoice.objects.create(
            invoice_number="INV-UNIQUE-001",
            total_amount=Decimal('100.00'),
            payment_method='cash'
        )
        
        # Creating another with same invoice_number should raise error
        with self.assertRaises(Exception):  # IntegrityError
            StoreInvoice.objects.create(
                invoice_number="INV-UNIQUE-001",
                total_amount=Decimal('200.00'),
                payment_method='cash'
            )


class StoreSaleModelTest(TestCase):
    """Test StoreSale model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.product = StoreProduct.objects.create(
            name="חולצה",
            category="ביגוד",
            cost_price=Decimal('50.00'),
            sale_price=Decimal('100.00'),
            branch=self.branch,
            stock_quantity=10
        )
        self.invoice = StoreInvoice.objects.create(
            invoice_number="INV-SALE-001",
            total_amount=Decimal('200.00'),
            payment_method='cash',
            payment_status='completed'
        )
        self.child = TestDataFactory.create_child()
    
    def test_create_store_sale(self):
        """Test creating a store sale"""
        sale = StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            child=self.child,
            quantity=2,
            unit_price=Decimal('100.00'),
            total_price=Decimal('200.00'),
            payment_method='cash',
            size="M"
        )
        
        self.assertEqual(sale.quantity, 2)
        self.assertEqual(sale.unit_price, Decimal('100.00'))
        self.assertEqual(sale.total_price, Decimal('200.00'))
        self.assertEqual(sale.size, "M")
    
    def test_store_sale_without_child(self):
        """Test store sale for walk-in customer (no child)"""
        sale = StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=1,
            unit_price=Decimal('100.00'),
            total_price=Decimal('100.00'),
            payment_method='cash'
        )
        
        self.assertIsNone(sale.child)
    
    def test_store_sale_multiple_items_same_invoice(self):
        """Test invoice can have multiple sale line items"""
        product2 = StoreProduct.objects.create(
            name="מכנסיים",
            category="ביגוד",
            cost_price=Decimal('70.00'),
            sale_price=Decimal('150.00')
        )
        
        sale1 = StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=1,
            unit_price=Decimal('100.00'),
            total_price=Decimal('100.00'),
            payment_method='cash'
        )
        
        sale2 = StoreSale.objects.create(
            invoice=self.invoice,
            product=product2,
            quantity=1,
            unit_price=Decimal('150.00'),
            total_price=Decimal('150.00'),
            payment_method='cash'
        )
        
        line_items = self.invoice.line_items.all()
        self.assertEqual(line_items.count(), 2)
        self.assertIn(sale1, line_items)
        self.assertIn(sale2, line_items)
    
    def test_store_sale_cascade_delete_with_invoice(self):
        """Test store sale is deleted when invoice is deleted"""
        sale = StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=1,
            unit_price=Decimal('100.00'),
            total_price=Decimal('100.00'),
            payment_method='cash'
        )
        
        sale_id = sale.id
        self.invoice.delete()
        
        with self.assertRaises(StoreSale.DoesNotExist):
            StoreSale.objects.get(id=sale_id)
    
    def test_store_sale_protect_delete_with_product(self):
        """Test store sale prevents product deletion (PROTECT)"""
        sale = StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=1,
            unit_price=Decimal('100.00'),
            total_price=Decimal('100.00'),
            payment_method='cash'
        )
        
        # Attempting to delete product should raise error because of PROTECT
        with self.assertRaises(Exception):  # ProtectedError
            self.product.delete()
