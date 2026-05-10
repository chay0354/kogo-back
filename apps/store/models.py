"""
Store Models - Product Inventory, Sales, and Invoices

Manages physical products, store sales, and invoice generation for the store.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone


class StoreProduct(models.Model):
    """
    Products available for sale in the store.
    
    Supports:
    - Multiple sizes (comma-separated)
    - Branch-specific or delivery products
    - Stock tracking with low-stock alerts
    - Cost and sale price tracking for profit calculation
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Product details
    name = models.CharField(
        max_length=100,
        verbose_name="שם מוצר",
        help_text="Product name"
    )
    category = models.CharField(
        max_length=50,
        default='כללי',
        verbose_name="קטגוריה",
        help_text="Product category"
    )
    size = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="מידות",
        help_text="Comma-separated sizes (e.g., S,M,L,XL)"
    )
    
    # Pricing
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="מחיר עלות",
        help_text="Cost price for profit calculation"
    )
    sale_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="מחיר מכירה",
        help_text="Sale price to customer"
    )
    
    # Location
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_products',
        verbose_name="סניף",
        help_text="Branch where product is located (null = delivery/online)"
    )
    
    # Inventory
    stock_quantity = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name="כמות במלאי",
        help_text="Current stock quantity"
    )
    min_stock_alert = models.IntegerField(
        default=3,
        validators=[MinValueValidator(0)],
        verbose_name="התראת מלאי מינימום",
        help_text="Alert when stock falls below this number"
    )
    
    # Additional info
    image_url = models.URLField(
        blank=True,
        null=True,
        verbose_name="תמונה",
        help_text="Product image URL"
    )
    notes = models.TextField(
        blank=True,
        verbose_name="הערות",
        help_text="Internal notes"
    )
    
    # Status
    is_active = models.BooleanField(
        default=True,
        verbose_name="פעיל",
        help_text="Active products appear in store"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")
    
    class Meta:
        db_table = 'store_products'
        verbose_name = "מוצר"
        verbose_name_plural = "מוצרים"
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['branch']),
            models.Index(fields=['stock_quantity']),
            models.Index(fields=['-created_at']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.category})"
    
    @property
    def is_low_stock(self):
        """Check if product is below minimum stock threshold."""
        return self.stock_quantity <= self.min_stock_alert
    
    @property
    def profit_margin(self):
        """Calculate profit margin percentage."""
        if self.cost_price == 0:
            return Decimal('0.00')
        return ((self.sale_price - self.cost_price) / self.sale_price) * 100

    def has_per_size_stock(self) -> bool:
        """True when stock is tracked per size on this product."""
        return self.size_stocks.exists()

    def recalculate_total_stock(self, save: bool = True):
        """
        Recompute stock_quantity from per-size stock rows.

        When a product has any size rows, stock_quantity becomes the sum of those
        rows so callers (analytics, list views, store dashboard) keep working
        without changes. If a product has no size rows, the existing
        stock_quantity value is left untouched.
        """
        if not self.has_per_size_stock():
            return self.stock_quantity
        total = sum(int(s.stock_quantity) for s in self.size_stocks.all())
        self.stock_quantity = max(0, total)
        if save:
            self.save(update_fields=['stock_quantity', 'updated_at'])
        return self.stock_quantity


class StoreProductSize(models.Model):
    """
    Per-size stock entries for a `StoreProduct`.

    A product may have many sizes (e.g. S, M, L). Each row tracks the stock
    quantity for one specific size of one specific product. When a product has
    at least one row, the parent's `stock_quantity` is treated as the running
    total of these rows; products without any rows continue to use the legacy
    single `stock_quantity` field.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        StoreProduct,
        on_delete=models.CASCADE,
        related_name='size_stocks',
        verbose_name="מוצר",
    )
    size = models.CharField(
        max_length=20,
        verbose_name="מידה",
        help_text="Size label (e.g. S, M, L, 42)",
    )
    stock_quantity = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name="כמות במלאי",
        help_text="Stock available for this specific size",
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        verbose_name="סדר",
        help_text="Display order among other sizes of the same product",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'store_product_sizes'
        verbose_name = "מלאי לפי מידה"
        verbose_name_plural = "מלאי לפי מידה"
        ordering = ['sort_order', 'size']
        unique_together = [('product', 'size')]
        indexes = [
            models.Index(fields=['product']),
            models.Index(fields=['product', 'size']),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.size}: {self.stock_quantity}"


class StoreInvoice(models.Model):
    """
    Invoices for store purchases.
    
    Generated for ALL payment methods (cash, credit card, monthly billing).
    Links to child for registered customers, or stores walk-in customer info.
    """
    PAYMENT_METHOD_CHOICES = [
        ('credit_card', 'אשראי'),
        ('cash', 'מזומן'),
        ('monthly_billing', 'הוראת קבע'),
    ]
    
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'ממתין'),
        ('completed', 'הושלם'),
        ('failed', 'נכשל'),
        ('refunded', 'זוכה'),
        ('refund_failed', 'זיכוי נכשל'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Invoice identification
    invoice_number = models.CharField(
        max_length=50,
        unique=True,
        verbose_name="מספר חשבונית",
        help_text="Auto-generated invoice number"
    )
    
    # Customer (child for registered, or walk-in info)
    child = models.ForeignKey(
        'customers.Child',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_invoices',
        verbose_name="ילד",
        help_text="Registered customer (null for walk-in)"
    )
    customer_name = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="שם לקוח",
        help_text="For walk-in customers without child record"
    )
    customer_phone = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="טלפון לקוח",
        help_text="For walk-in customers"
    )
    
    # Payment details
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="סכום כולל"
    )
    refunded_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="סכום שזוכה",
        help_text="Amount that has been refunded"
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        verbose_name="אמצעי תשלום"
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='pending',
        verbose_name="סטטוס תשלום"
    )
    
    # Tranzila integration
    tranzila_txn = models.ForeignKey(
        'customers.TranzilaTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_invoices',
        verbose_name="עסקת טרנזילה",
        help_text="Link to full Tranzila transaction record"
    )
    tranzila_transaction_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="מזהה עסקה טרנזילה",
        help_text="Deprecated: Use tranzila_txn FK instead"
    )
    tranzila_confirmation_code = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="קוד אישור טרנזילה"
    )
    charged_with_token = models.BooleanField(
        default=False,
        verbose_name="חויב עם טוקן",
        help_text="True if charged using stored token (no iframe)"
    )
    
    # Location and metadata
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_invoices',
        verbose_name="סניף"
    )
    issue_date = models.DateTimeField(
        auto_now_add=True,
        verbose_name="תאריך הנפקה"
    )
    notes = models.TextField(
        blank=True,
        verbose_name="הערות"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    
    class Meta:
        db_table = 'store_invoices'
        verbose_name = "חשבונית חנות"
        verbose_name_plural = "חשבוניות חנות"
        ordering = ['-issue_date']
        indexes = [
            models.Index(fields=['invoice_number']),
            models.Index(fields=['child']),
            models.Index(fields=['payment_status']),
            models.Index(fields=['-issue_date']),
        ]
    
    def __str__(self):
        customer = self.child.full_name if self.child else self.customer_name or 'Walk-in'
        return f"{self.invoice_number} - {customer} - ₪{self.total_amount}"
    
    def save(self, *args, **kwargs):
        # Auto-generate invoice number if not set
        if not self.invoice_number:
            # Format: INV-YYYYMM-XXXXX
            today = timezone.now()
            prefix = f"INV-{today.strftime('%Y%m')}"
            
            # Get last invoice number for this month
            last_invoice = StoreInvoice.objects.filter(
                invoice_number__startswith=prefix
            ).order_by('-invoice_number').first()
            
            if last_invoice:
                try:
                    last_num = int(last_invoice.invoice_number.split('-')[-1])
                    next_num = last_num + 1
                except (ValueError, IndexError):
                    next_num = 1
            else:
                next_num = 1
            
            self.invoice_number = f"{prefix}-{next_num:05d}"
        
        super().save(*args, **kwargs)


class StoreSale(models.Model):
    """
    Individual line items for store sales.
    
    Each sale is linked to an invoice and represents one product purchase.
    Multiple sales can be part of the same invoice.
    """
    PAYMENT_METHOD_CHOICES = [
        ('credit_card', 'אשראי'),
        ('cash', 'מזומן'),
        ('monthly_billing', 'הוראת קבע'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationships
    invoice = models.ForeignKey(
        StoreInvoice,
        on_delete=models.CASCADE,
        related_name='line_items',
        verbose_name="חשבונית"
    )
    product = models.ForeignKey(
        StoreProduct,
        on_delete=models.PROTECT,
        related_name='sales',
        verbose_name="מוצר"
    )
    child = models.ForeignKey(
        'customers.Child',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_purchases',
        verbose_name="ילד"
    )
    
    # Sale details
    quantity = models.IntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="כמות"
    )
    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="מחיר יחידה"
    )
    total_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="סכום כולל"
    )
    size = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="מידה"
    )
    
    # Payment and location
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        verbose_name="אמצעי תשלום"
    )
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_sales',
        verbose_name="סניף"
    )
    
    # Additional info
    notes = models.TextField(
        blank=True,
        default='',
        verbose_name="הערות"
    )
    
    # Timestamps
    sale_date = models.DateTimeField(
        auto_now_add=True,
        verbose_name="תאריך מכירה"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    
    class Meta:
        db_table = 'store_sales'
        verbose_name = "מכירה"
        verbose_name_plural = "מכירות"
        ordering = ['-sale_date']
        indexes = [
            models.Index(fields=['product']),
            models.Index(fields=['child']),
            models.Index(fields=['-sale_date']),
        ]
    
    def __str__(self):
        return f"{self.product.name} x{self.quantity} - ₪{self.total_price}"

