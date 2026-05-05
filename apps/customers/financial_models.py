"""
Financial Models - Invoices and Discounts
"""
import uuid
from django.db import models
from apps.core.models import Branch
from apps.customers.models import Family, Parent, Child
from apps.courses.models import Course, Lesson


class Invoice(models.Model):
    """חשבוניות"""
    STATUS_CHOICES = [
        ('pending', 'ממתין'),
        ('paid', 'שולם'),
        ('failed', 'נכשל'),
        ('cancelled', 'בוטל'),
        ('credit', 'זיכוי'),
    ]
    
    PAYMENT_METHOD_CHOICES = [
        ('credit_card', 'כרטיס אשראי'),
        ('cash', 'מזומן'),
        ('bank_transfer', 'העברה בנקאית'),
        ('check', 'צ\'ק'),
    ]
    
    PAYMENT_TYPE_CHOICES = [
        ('recurring', 'מנוי חוזר'),
        ('one_time', 'חד-פעמי'),
        ('manual', 'ידני'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=50, unique=True, verbose_name="מספר חשבונית")
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='invoices', verbose_name="משפחה")
    parent = models.ForeignKey(Parent, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices', verbose_name="הורה")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, related_name='invoices', verbose_name="סניף")
    
    # Link to Payment model (NEW)
    payment = models.ForeignKey(
        'Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices',
        verbose_name="תשלום"
    )
    
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="סכום")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="סטטוס")
    payment_method = models.CharField(max_length=50, choices=PAYMENT_METHOD_CHOICES, blank=True, verbose_name="אמצעי תשלום")
    
    # Payment type (NEW)
    payment_type = models.CharField(
        max_length=20,
        choices=PAYMENT_TYPE_CHOICES,
        default='manual',
        verbose_name="סוג תשלום"
    )
    
    payer_name = models.CharField(max_length=200, blank=True, verbose_name="שם משלם")
    payer_email = models.EmailField(blank=True, verbose_name="אימייל משלם")
    payer_phone = models.CharField(max_length=20, blank=True, verbose_name="טלפון משלם")
    
    # External payment system IDs
    meshulam_id = models.CharField(max_length=100, blank=True, verbose_name="מזהה משולם")
    tranzila_transaction_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="מזהה עסקת טרנזילה"
    )
    external_transaction_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="מזהה עסקה חיצוני"
    )
    
    pdf_url = models.URLField(blank=True, verbose_name="קישור PDF")
    invoice_date = models.DateTimeField(verbose_name="תאריך חשבונית")
    email_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="נשלח במייל")
    whatsapp_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="נשלח בוואטסאפ")
    admin_notes = models.TextField(blank=True, verbose_name="הערות מנהל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'invoices'
        verbose_name = "חשבונית"
        verbose_name_plural = "חשבוניות"
        ordering = ['-invoice_date']

    def __str__(self):
        return f"חשבונית {self.invoice_number} - {self.family.name}"


class InvoiceChild(models.Model):
    """ילדים בחשבונית"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='children', verbose_name="חשבונית")
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='invoice_entries', verbose_name="ילד")
    course = models.ForeignKey(Course, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_entries', verbose_name="חוג")
    lesson = models.ForeignKey(
        Lesson, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='invoice_entries', 
        verbose_name="שיעור",
        help_text="The specific lesson this invoice item is for"
    )
    product = models.ForeignKey(
        'store.StoreProduct',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_entries',
        verbose_name="מוצר",
        help_text="The product this invoice item is for (if not a lesson)"
    )

    class Meta:
        db_table = 'invoice_children'
        verbose_name = "ילד בחשבונית"
        verbose_name_plural = "ילדים בחשבונית"

    def __str__(self):
        return f"{self.child.full_name} - {self.invoice.invoice_number}"


class InvoiceActivityLog(models.Model):
    """לוג פעילות חשבונית"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='activity_logs', verbose_name="חשבונית")
    action = models.CharField(max_length=100, verbose_name="פעולה")
    details = models.JSONField(blank=True, null=True, verbose_name="פרטים")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'invoice_activity_log'
        verbose_name = "לוג חשבונית"
        verbose_name_plural = "לוגים חשבוניות"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice.invoice_number} - {self.action}"


class Discount(models.Model):
    """הנחות"""
    DISCOUNT_TYPE_CHOICES = [
        ('percentage', 'אחוז'),
        ('fixed', 'סכום קבוע'),
        ('fixed_final_price', 'מחיר סופי קבוע'),
    ]
    
    APPLIES_TO_CHOICES = [
        ('family', 'משפחה'),
        ('child', 'ילד'),
        ('course', 'חוג'),
        ('lesson', 'שיעור'),
    ]
    
    PROMOTION_TYPE_CHOICES = [
        ('permanent', 'קבוע'),
        ('temporary', 'זמני'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="שם ההנחה")
    description = models.TextField(blank=True, verbose_name="תיאור")
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES, verbose_name="סוג")
    value = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="ערך")
    applies_to = models.CharField(max_length=20, choices=APPLIES_TO_CHOICES, verbose_name="חל על")
    promotion_type = models.CharField(max_length=20, choices=PROMOTION_TYPE_CHOICES, verbose_name="סוג מבצע")
    start_date = models.DateField(null=True, blank=True, verbose_name="תאריך התחלה")
    end_date = models.DateField(null=True, blank=True, verbose_name="תאריך סיום")
    is_built_in = models.BooleanField(default=False, verbose_name="הנחה מובנית")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'discounts'
        verbose_name = "הנחה"
        verbose_name_plural = "הנחות"
        ordering = ['name']

    def __str__(self):
        return self.name


class BranchDiscountMetrics(models.Model):
    """מדדי הנחות לפי סניף - Tracks total discounts given per branch per month"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='discount_metrics', verbose_name="סניף")
    month = models.DateField(verbose_name="חודש", help_text="First day of the month")
    
    # Discount totals
    total_discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="סה״כ הנחות",
        help_text="Total amount of discounts given this month"
    )
    discount_count = models.PositiveIntegerField(
        default=0,
        verbose_name="מספר הנחות",
        help_text="Number of discounts applied this month"
    )
    
    # Breakdown by discount type
    early_signup_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="הנחות רישום מוקדם"
    )
    second_child_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="הנחות ילד שני"
    )
    fixed_price_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="הנחות מחיר קבוע"
    )
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")
    
    class Meta:
        db_table = 'branch_discount_metrics'
        verbose_name = "מדדי הנחות סניף"
        verbose_name_plural = "מדדי הנחות סניפים"
        ordering = ['-month', 'branch']
        unique_together = [['branch', 'month']]
        indexes = [
            models.Index(fields=['branch', 'month']),
            models.Index(fields=['-month']),
        ]
    
    def __str__(self):
        return f"{self.branch.name} - {self.month.strftime('%Y-%m')} - ₪{self.total_discount_amount}"

