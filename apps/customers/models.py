import uuid
from django.db import models
from django.utils import timezone
from apps.core.models import Branch


class Family(models.Model):
    """משפחות"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="שם משפחה")
    phone = models.CharField(max_length=20, verbose_name="טלפון")
    email = models.EmailField(blank=True, verbose_name="אימייל")
    address = models.TextField(blank=True, verbose_name="כתובת")
    parent_id_number = models.CharField(max_length=20, blank=True, verbose_name="ת.ז. הורה")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, related_name='families', verbose_name="סניף")
    notes = models.TextField(blank=True, verbose_name="הערות")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'families'
        verbose_name = "משפחה"
        verbose_name_plural = "משפחות"
        ordering = ['name']

    def __str__(self):
        return self.name


class Parent(models.Model):
    """הורים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='parents', verbose_name="משפחה")
    first_name = models.CharField(max_length=100, verbose_name="שם פרטי")
    last_name = models.CharField(max_length=100, verbose_name="שם משפחה")
    phone = models.CharField(max_length=20, verbose_name="טלפון")
    email = models.EmailField(blank=True, verbose_name="אימייל")
    is_primary = models.BooleanField(default=False, verbose_name="הורה ראשי")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'parents'
        verbose_name = "הורה"
        verbose_name_plural = "הורים"
        ordering = ['family', '-is_primary', 'first_name']

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        """
        USAGE: Used in ParentSerializer (apps/customers/serializers.py)
        USAGE: Used in Django admin displays
        """
        return f"{self.first_name} {self.last_name}"


class Child(models.Model):
    """ילדים/תלמידים"""
    GENDER_CHOICES = [
        ('male', 'זכר'),
        ('female', 'נקבה'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'פעיל'),
        ('trial_signed', 'נרשם לניסיון'),
        ('trial_completed', 'ביצע ניסיון'),
        ('payment_problem', 'בעיות באשראי'),
        ('not_paid', 'לא שולם'),
        ('pending', 'בתהליך רישום'),
        ('ghost', 'רפאים'),
        ('inactive', 'לא פעיל'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='children', verbose_name="משפחה")
    first_name = models.CharField(max_length=100, verbose_name="שם פרטי")
    last_name = models.CharField(max_length=100, verbose_name="שם משפחה")
    id_number = models.CharField(max_length=20, blank=True, verbose_name="ת.ז.")
    phone_number = models.CharField(max_length=20, blank=True, verbose_name="טלפון")
    birth_date = models.DateField(verbose_name="תאריך לידה")
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, verbose_name="מין")
    
    # NEW: Explicit status field
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending', 
        verbose_name="סטטוס"
    )
    
    # Absence tracking
    absent_irregularly = models.BooleanField(
        default=False,
        verbose_name="היעדרות חריגה",
        help_text="מסומן כ-True אם הילד נעדר 3 פעמים עם פחות מ-8 ימים בין ההיעדרויות"
    )
    
    # Payment tracking
    paid_until_date = models.DateField(
        null=True, 
        blank=True, 
        verbose_name="שולם עד תאריך",
        help_text="התאריך עד אליו התשלום בתוקף"
    )
    
    # Trial tracking
    trial_classes_attended = models.PositiveIntegerField(
        default=0,
        verbose_name="שיעורי ניסיון",
        help_text="כמות שיעורי ניסיון שהילד השתתף בהם"
    )
    
    # Subscription dates (kept for reference)
    subscription_start_date = models.DateField(null=True, blank=True, verbose_name="תחילת מנוי")
    subscription_end_date = models.DateField(null=True, blank=True, verbose_name="סיום מנוי")
    
    notes = models.TextField(blank=True, verbose_name="הערות")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'children'
        verbose_name = "ילד"
        verbose_name_plural = "ילדים"
        ordering = ['family', 'first_name']

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        """
        USAGE: Used extensively in serializers and admin displays
        USAGE: ChildSerializer, ChildWithDetailsSerializer, EnrollmentSerializer
        """
        return f"{self.first_name} {self.last_name}"

    @property
    def age(self):
        """
        מחשב גיל בשנים
        
        USAGE: Used in ChildSerializer and ChildWithDetailsSerializer
        USAGE: Displayed in Django admin list_display
        """
        from datetime import date
        today = date.today()
        return today.year - self.birth_date.year - ((today.month, today.day) < (self.birth_date.month, self.birth_date.day))
    
    def calculate_status(self):
        """
        מחשב את הסטטוס האמיתי על סמך תאריכים ותשלומים
        
        USAGE: Called by update_status() method
        USAGE: Used in tests (test_child_status.py)
        """
        from datetime import date
        today = date.today()
        
        # Priority 1: Subscription expired (black)
        if self.subscription_end_date and today > self.subscription_end_date:
            return 'expired'
        
        # Priority 2: Has subscription but no paid_until_date or overdue (red)
        if self.subscription_start_date:
            if not self.paid_until_date or today > self.paid_until_date:
                return 'payment_problem'
            
            # Priority 3: Has subscription and paid (green)
            if today <= self.paid_until_date:
                return 'active'
        
        # Priority 4: No subscription = trial (orange)
        return 'trial'
    
    def update_status(self, save=True):
        """
        עדכן את הסטטוס על סמך החישוב
        
        USAGE: Called from ChildViewSet.update_status() API endpoint
        USAGE: Called from Django admin action (update_status_action)
        USAGE: Called internally by add_payment_month()
        USAGE: Used in tests
        """
        self.status = self.calculate_status()
        if save:
            self.save(update_fields=['status', 'updated_at'])


# ============================================================================
# Payment Models - Tranzila Payment Integration
# ============================================================================

class Payment(models.Model):
    """
    תשלומים - Tracks all payments (one-time and recurring)
    
    Each payment represents a single transaction attempt or completion.
    For recurring payments, the initial setup creates a Payment record,
    and subsequent charges also create their own Payment records.
    """
    PAYMENT_TYPE_CHOICES = [
        ('recurring_subscription', 'מנוי חוזר'),
        ('one_time', 'תשלום חד-פעמי'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'ממתין'),
        ('processing', 'בעיבוד'),
        ('completed', 'הושלם'),
        ('failed', 'נכשל'),
        ('refunded', 'הוחזר'),
        ('cancelled', 'בוטל'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Core relationships
    child = models.ForeignKey(
        Child, 
        on_delete=models.CASCADE, 
        related_name='payments', 
        verbose_name="ילד"
    )
    parent = models.ForeignKey(
        Parent, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='payments', 
        verbose_name="הורה"
    )
    family = models.ForeignKey(
        Family, 
        on_delete=models.CASCADE, 
        related_name='payments', 
        verbose_name="משפחה"
    )
    branch = models.ForeignKey(
        Branch, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='payments', 
        verbose_name="סניף"
    )
    lesson = models.ForeignKey(
        'courses.Lesson',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        verbose_name="שיעור",
        help_text="The lesson this payment is for (if applicable)"
    )
    product = models.ForeignKey(
        'store.StoreProduct',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        verbose_name="מוצר",
        help_text="The product this payment is for (if applicable)"
    )
    
    # Payment details
    payment_type = models.CharField(
        max_length=30, 
        choices=PAYMENT_TYPE_CHOICES, 
        verbose_name="סוג תשלום"
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending', 
        verbose_name="סטטוס"
    )
    
    # Amounts
    base_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        verbose_name="סכום בסיס",
        help_text="המחיר המקורי לפני הנחות"
    )
    discount_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0.00,
        verbose_name="סכום הנחה",
        help_text="סכום ההנחות שהוחלו"
    )
    final_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        verbose_name="סכום סופי",
        help_text="הסכום הסופי לחיוב אחרי הנחות"
    )
    
    # Tranzila reference
    tranzila_transaction = models.ForeignKey(
        'TranzilaTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        verbose_name="עסקת טרנזילה"
    )
    
    # Additional metadata
    description = models.TextField(blank=True, verbose_name="תיאור")
    payment_date = models.DateTimeField(null=True, blank=True, verbose_name="תאריך תשלום")
    
    # Failure tracking
    failure_reason = models.TextField(blank=True, verbose_name="סיבת כישלון")
    failure_code = models.CharField(max_length=50, blank=True, verbose_name="קוד שגיאה")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")
    
    class Meta:
        db_table = 'payments'
        verbose_name = "תשלום"
        verbose_name_plural = "תשלומים"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['child', 'status']),
            models.Index(fields=['family', 'status']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['-payment_date']),
        ]
    
    def __str__(self):
        return f"תשלום {self.id} - {self.child.full_name} - ₪{self.final_amount}"


class RecurringPayment(models.Model):
    """
    מנויים חוזרים - Tracks recurring payment subscriptions
    
    Represents an ongoing subscription with Tranzila.
    Each recurring payment can have multiple Payment records (one per charge).
    """
    STATUS_CHOICES = [
        ('active', 'פעיל'),
        ('paused', 'מושהה'),
        ('cancelled', 'מבוטל'),
        ('expired', 'פג תוקף'),
        ('failed', 'נכשל'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Core relationships
    child = models.ForeignKey(
        Child, 
        on_delete=models.CASCADE, 
        related_name='recurring_payments', 
        verbose_name="ילד"
    )
    initial_payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='recurring_subscription',
        verbose_name="תשלום ראשוני"
    )
    
    # Tranzila recurring details
    tranzila_token = models.CharField(
        max_length=255, 
        blank=True,
        verbose_name="טוקן טרנזילה",
        help_text="Token for recurring charges"
    )
    tranzila_recurring_index = models.CharField(
        max_length=100, 
        blank=True,
        verbose_name="אינדקס חוזר",
        help_text="Tranzila recurring payment index"
    )
    card_expire_month = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="חודש תפוגה",
        help_text="Card expiration month (1-12)"
    )
    card_expire_year = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="שנת תפוגה",
        help_text="Card expiration year (e.g., 2027)"
    )
    
    # Status
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='active', 
        verbose_name="סטטוס"
    )
    
    # Billing details
    base_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="סכום בסיס",
        help_text="Original lesson price before discounts"
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        verbose_name="סכום הנחה",
        help_text="Total discount amount applied when this recurring payment was created"
    )
    amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        verbose_name="סכום חיוב",
        help_text="Final recurring charge amount (base_amount - discount_amount)"
    )
    discount_details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="פרטי הנחות",
        help_text="Snapshot of discounts applied: [{name, type, value, reason}, ...]"
    )
    billing_day = models.PositiveIntegerField(
        default=1,
        verbose_name="יום חיוב בחודש",
        help_text="Day of month for recurring charges (1-28)"
    )
    
    # Dates
    start_date = models.DateField(verbose_name="תאריך התחלה")
    end_date = models.DateField(
        null=True, 
        blank=True, 
        verbose_name="תאריך סיום"
    )
    next_billing_date = models.DateField(
        null=True, 
        blank=True,
        verbose_name="תאריך חיוב הבא"
    )
    last_charge_date = models.DateField(
        null=True, 
        blank=True,
        verbose_name="תאריך חיוב אחרון"
    )
    
    # Cancellation tracking
    cancelled_at = models.DateTimeField(
        null=True, 
        blank=True, 
        verbose_name="תאריך ביטול"
    )
    cancellation_reason = models.TextField(
        blank=True, 
        verbose_name="סיבת ביטול"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")
    
    class Meta:
        db_table = 'recurring_payments'
        verbose_name = "מנוי חוזר"
        verbose_name_plural = "מנויים חוזרים"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['child', 'status']),
            models.Index(fields=['status', 'next_billing_date']),
            models.Index(fields=['tranzila_token']),
        ]
    
    def __str__(self):
        return f"מנוי {self.child.full_name} - ₪{self.amount} - {self.get_status_display()}"


class TranzilaTransaction(models.Model):
    """
    עסקאות טרנזילה - Raw storage of Tranzila API responses
    
    Stores complete transaction details from Tranzila for audit trail,
    debugging, and idempotency checking.
    """
    TRANSACTION_TYPE_CHOICES = [
        ('authorization', 'אישור'),
        ('charge', 'חיוב'),
        ('refund', 'זיכוי'),
        ('recurring_setup', 'הגדרת חוזר'),
        ('recurring_charge', 'חיוב חוזר'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Tranzila identifiers
    transaction_id = models.CharField(
        max_length=100, 
        db_index=True,
        verbose_name="מזהה עסקה",
        help_text="Tranzila transaction ID"
    )
    confirmation_code = models.CharField(
        max_length=100, 
        blank=True,
        verbose_name="קוד אישור"
    )
    
    # Transaction details
    transaction_type = models.CharField(
        max_length=30, 
        choices=TRANSACTION_TYPE_CHOICES,
        verbose_name="סוג עסקה"
    )
    response_code = models.CharField(
        max_length=10, 
        blank=True,
        verbose_name="קוד תגובה",
        help_text="Tranzila response code (000 = success)"
    )
    response_message = models.TextField(
        blank=True, 
        verbose_name="הודעת תגובה"
    )
    
    # Raw data storage
    request_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="נתוני בקשה",
        help_text="Raw request sent to Tranzila"
    )
    response_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="נתוני תגובה",
        help_text="Raw response from Tranzila"
    )
    
    # Idempotency
    idempotency_key = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        verbose_name="מפתח ייחודיות",
        help_text="Unique key to prevent duplicate webhook processing"
    )
    
    # Status
    is_successful = models.BooleanField(
        default=False, 
        verbose_name="הצליח"
    )
    
    # Timestamps
    request_timestamp = models.DateTimeField(
        default=timezone.now,
        verbose_name="זמן בקשה"
    )
    response_timestamp = models.DateTimeField(
        null=True, 
        blank=True,
        verbose_name="זמן תגובה"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    
    class Meta:
        db_table = 'tranzila_transactions'
        verbose_name = "עסקת טרנזילה"
        verbose_name_plural = "עסקאות טרנזילה"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['transaction_id']),
            models.Index(fields=['idempotency_key']),
            models.Index(fields=['-created_at']),
        ]
    
    def __str__(self):
        return f"Tranzila {self.transaction_id} - {self.get_transaction_type_display()}"


class PaymentDiscountSnapshot(models.Model):
    """
    צילום הנחות בתשלום - Snapshot of discounts applied to a payment
    
    Captures the exact discount details at the time of payment
    for historical accuracy and audit trail.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name='discount_snapshots',
        verbose_name="תשלום"
    )
    discount = models.ForeignKey(
        'Discount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_snapshots',
        verbose_name="הנחה"
    )
    
    # Snapshot of discount details
    discount_name = models.CharField(
        max_length=200, 
        verbose_name="שם הנחה"
    )
    discount_type = models.CharField(
        max_length=50, 
        verbose_name="סוג הנחה"
    )
    discount_value = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        verbose_name="ערך הנחה",
        help_text="Original discount value (percentage or fixed)"
    )
    amount_deducted = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        verbose_name="סכום שהופחת",
        help_text="Actual amount deducted from payment"
    )
    
    # Context
    reason = models.TextField(
        blank=True, 
        verbose_name="סיבה",
        help_text="Why this discount was applied"
    )
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    
    class Meta:
        db_table = 'payment_discount_snapshots'
        verbose_name = "צילום הנחה"
        verbose_name_plural = "צילומי הנחות"
        ordering = ['payment', 'created_at']
    
    def __str__(self):
        return f"{self.discount_name} - ₪{self.amount_deducted}"

