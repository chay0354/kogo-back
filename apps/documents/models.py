import uuid
from django.db import models
from django.db import transaction


DOCUMENT_TYPE_CHOICES = [
    ('tax_invoice', 'חשבונית מס'),
    ('receipt', 'קבלה'),
    ('combined', 'חשבונית מס/קבלה'),
    ('transaction_invoice', 'חשבונית עסקה'),
    ('credit_invoice', 'חשבונית מס זיכוי'),
    # Not a tax document: no fiscal number, never sent to Tranzila.
    ('draft', 'טיוטה'),
]

TRANZILA_DOCUMENT_TYPE = {
    'tax_invoice': 'IN',
    'receipt': 'RE',
    'combined': 'IR',
    'transaction_invoice': 'DI',
}

PAYMENT_METHOD_CHOICES = [
    ('cash', 'מזומן'),
    ('check', "צ'ק"),
    ('credit_card', 'אשראי'),
    ('bank_transfer', 'העברה בנקאית'),
]

CURRENCY_CHOICES = [
    ('ILS', 'שקל ₪'),
    ('USD', 'דולר $'),
    ('EUR', 'אירו €'),
]


class DocumentCounter(models.Model):
    """Global sequential counter per year — one sequence across all document types."""
    year = models.PositiveIntegerField(unique=True)
    counter = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'document_counters'

    @classmethod
    def next_number(cls, year: int) -> int:
        with transaction.atomic():
            obj, _ = cls.objects.select_for_update().get_or_create(
                year=year,
                defaults={'counter': 0},
            )
            obj.counter += 1
            obj.save(update_fields=['counter'])
            return obj.counter


class DocumentSeries(models.Model):
    """
    One consecutive number series per document kind and tax year.

    סעיף 18(א)(3) להוראות ניהול פנקסי חשבונות: consecutive numbers that never
    repeat within a tax year. The receipts issued for lesson charges used to be
    numbered from a payment's UUID, which is unique but not consecutive, so
    nobody — the software included (נספח ה׳(א)(5)) — could show the run is whole.
    """
    series = models.CharField(max_length=10, verbose_name="סדרה")
    year = models.PositiveIntegerField(verbose_name="שנת מס")
    counter = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'document_series'
        unique_together = [('series', 'year')]

    def __str__(self) -> str:
        return f'{self.series}-{self.year}: {self.counter}'

    @classmethod
    def next_number(cls, series: str, year: int) -> int:
        """The next number, under a row lock; a caller that rolls back gives it back."""
        with transaction.atomic():
            row, _ = cls.objects.select_for_update().get_or_create(
                series=series, year=year, defaults={'counter': 0},
            )
            row.counter += 1
            row.save(update_fields=['counter'])
            return row.counter


class FormalDocument(models.Model):
    """מסמך פיננסי רשמי — tax invoice, receipt, combined, transaction invoice, or credit note."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_number = models.CharField(max_length=30, unique=True, verbose_name="מספר מסמך")
    document_type = models.CharField(
        max_length=30, choices=DOCUMENT_TYPE_CHOICES, verbose_name="סוג מסמך"
    )

    # Client — one of these is set
    client_type = models.CharField(
        max_length=20,
        choices=[('business', 'עסקי'), ('existing', 'קיים')],
        verbose_name="סוג לקוח",
    )
    child = models.ForeignKey(
        'customers.Child',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='formal_documents',
        verbose_name="ילד",
    )
    business_customer = models.ForeignKey(
        'customers.BusinessCustomer',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='formal_documents',
        verbose_name="לקוח עסקי",
    )

    # Document metadata
    document_date = models.DateField(verbose_name="תאריך מסמך")
    due_date = models.DateField(null=True, blank=True, verbose_name="תאריך פירעון")
    description = models.TextField(blank=True, verbose_name="פרטים")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='ILS', verbose_name="מטבע")
    prices_include_vat = models.BooleanField(default=False, verbose_name="מחירים כוללים מע\"מ")
    payment_terms = models.CharField(max_length=30, blank=True, verbose_name="תנאי תשלום")

    # VAT
    vat_exempt = models.BooleanField(default=False, verbose_name="פטור ממע\"מ")
    vat_percent = models.DecimalField(max_digits=5, decimal_places=2, default=18, verbose_name="אחוז מע\"מ")

    # Amounts (stored in document currency)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="סכום לפני הנחה")
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="הנחה בשקלים")
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name="הנחה באחוזים")
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="סכום מע\"מ")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="סה\"כ")

    # Notes
    customer_notes = models.TextField(blank=True, verbose_name="הערות ללקוח")
    internal_notes = models.TextField(blank=True, verbose_name="הערה פנימית")

    # Credit note link
    linked_document = models.ForeignKey(
        'self',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='credit_notes',
        verbose_name="מסמך מקושר",
    )
    # For credit invoices: manual invoice number if linked_document not resolved
    linked_document_number = models.CharField(max_length=30, blank=True, verbose_name="מספר חשבונית מקושרת")
    credit_reason = models.TextField(blank=True, verbose_name="סיבת זיכוי")
    # סעיף 9(ה)(4): a credit note names the original's number AND date. The
    # original is often not a FormalDocument (a lesson receipt, a store sale),
    # so its date is kept here rather than reached through linked_document.
    linked_document_date = models.DateField(null=True, blank=True, verbose_name="תאריך המסמך המקורי")
    # The customer when there is no child or business customer to point at —
    # a walk-in store buyer whose sale is credited. Nullable: expand-only.
    customer_name = models.CharField(max_length=200, null=True, blank=True, verbose_name="שם לקוח")
    # For drafts: the document type it becomes when approved.
    draft_target_type = models.CharField(max_length=30, blank=True, verbose_name="סוג מסמך לאחר אישור")

    # Income tagging: explicit, or inherited from the business customer.
    business = models.ForeignKey(
        'core.Business', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='documents', verbose_name="עסק",
    )
    business_category = models.ForeignKey(
        'core.BusinessCategory', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='documents', verbose_name="קטגוריה בעסק",
    )
    # Tranzila document data
    tranzila_doc_id = models.CharField(max_length=100, blank=True, verbose_name="מזהה מסמך טרנזילה")
    tranzila_retrieval_key = models.CharField(max_length=100, blank=True, verbose_name="מפתח אחזור טרנזילה")
    pdf_url = models.URLField(blank=True, verbose_name="קישור PDF")
    tranzila_issued = models.BooleanField(default=False, verbose_name="הופק בטרנזילה")

    # מספר הקצאה — Israel Tax Authority allocation number.
    #
    # Above the threshold the customer cannot deduct input VAT without it, and
    # the number is obtained from the Tax Authority, not invented here. Until
    # there is an API integration it is fetched by hand from their portal and
    # typed in, which is why it lives on the document rather than being derived:
    # a number nobody can reproduce from the invoice must be stored with it.
    allocation_number = models.CharField(
        max_length=20, blank=True, verbose_name="מספר הקצאה",
        help_text="9 הספרות שהתקבלו מרשות המסים. מודפס על המסמך ומדווח ב-PCN874.",
    )
    allocation_entered_at = models.DateTimeField(null=True, blank=True, verbose_name="מועד הזנת ההקצאה")
    allocation_entered_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='allocation_numbers_entered', verbose_name="מי הזין",
    )

    # Meta
    branch = models.ForeignKey(
        'core.Branch',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name="סניף",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'formal_documents'
        verbose_name = "מסמך פיננסי"
        verbose_name_plural = "מסמכים פיננסיים"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.document_number} ({self.get_document_type_display()})"


class DocumentLineItem(models.Model):
    """שורת פריט במסמך."""
    document = models.ForeignKey(
        FormalDocument, on_delete=models.CASCADE, related_name='line_items'
    )
    sku = models.CharField(max_length=50, blank=True, verbose_name='מק"ט')
    description = models.CharField(max_length=500, blank=True, verbose_name="תיאור")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1, verbose_name="כמות")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="מחיר יחידה")
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='סה"כ שורה')

    class Meta:
        db_table = 'document_line_items'
        verbose_name = "שורת פריט"
        verbose_name_plural = "שורות פריטים"
        ordering = ['id']

    def save(self, *args, **kwargs):
        self.line_total = self.quantity * self.unit_price
        super().save(*args, **kwargs)


class DocumentPayment(models.Model):
    """אמצעי תשלום במסמך (קבלה / חשבונית מס/קבלה)."""
    document = models.ForeignKey(
        FormalDocument, on_delete=models.CASCADE, related_name='payments'
    )
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES, verbose_name="אמצעי תשלום"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="סכום")

    # Per-method reference fields
    reference = models.CharField(max_length=200, blank=True, verbose_name="אסמכתא / מספר")
    notes = models.TextField(blank=True, verbose_name="הערות")

    # Check-specific
    check_date = models.DateField(null=True, blank=True, verbose_name="תאריך צ'ק")
    check_bank = models.CharField(max_length=100, blank=True, verbose_name="בנק")
    check_branch = models.CharField(max_length=50, blank=True, verbose_name="סניף")
    check_account = models.CharField(max_length=50, blank=True, verbose_name="מספר חשבון")

    # Card-specific
    card_last_four = models.CharField(max_length=4, blank=True, verbose_name="4 ספרות אחרונות")
    card_expiry = models.CharField(max_length=7, blank=True, verbose_name="תוקף")
    card_installments = models.PositiveSmallIntegerField(default=1, verbose_name="מספר תשלומים")

    class Meta:
        db_table = 'document_payments'
        verbose_name = "תשלום במסמך"
        verbose_name_plural = "תשלומים במסמך"


class CashPlan(models.Model):
    """
    מנוי במזומן — cash paid up front, recognised month by month.

    A parent pays the whole year in cash. The money arrives once, so a receipt
    for the whole sum is issued the moment it is taken; the income belongs to
    the months it covers, so a document is issued on the 1st of each of them.
    This is the same shape the office already uses for a series of post-dated
    checks (CheckPlan), with one difference: a check carries its own date and
    amount, while cash is one sum split into equal months.

    `monthly_document_type` is a choice and not a constant. The check series
    issues a tax invoice for each month precisely because the money was already
    receipted once, and receipting it again would count the same shekels twice;
    the owner asked for חשבונית מס/קבלה here, so that is the default, and the
    other option is one field away when their accountant rules on it.
    """
    STATUS_CHOICES = [
        ('active', 'פעיל'),
        ('completed', 'הושלם'),
        ('cancelled', 'בוטל'),
    ]
    MONTHLY_DOCUMENT_CHOICES = [
        ('combined', 'חשבונית מס/קבלה'),
        ('tax_invoice', 'חשבונית מס'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey(
        'customers.Child', on_delete=models.CASCADE,
        related_name='cash_plans', verbose_name="ילד",
    )
    lesson = models.ForeignKey(
        'courses.Lesson', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cash_plans', verbose_name="שיעור",
    )
    description = models.CharField(max_length=300, blank=True, verbose_name="תיאור")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', verbose_name="סטטוס")

    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="סכום ששולם במזומן",
    )
    monthly_amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="סכום חודשי",
        help_text="מחיר החוג הרגיל — הסכום שיופיע על המסמך של כל חודש.",
    )
    monthly_document_type = models.CharField(
        max_length=20, choices=MONTHLY_DOCUMENT_CHOICES, default='combined',
        verbose_name="סוג המסמך החודשי",
    )

    # The receipt for the whole sum, issued at registration.
    receipt = models.ForeignKey(
        FormalDocument, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cash_plan_receipt', verbose_name="קבלה",
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="סניף",
    )
    created_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cash_plans_created', verbose_name="נרשם על ידי",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'cash_plans'
        verbose_name = "מנוי במזומן"
        verbose_name_plural = "מנויים במזומן"
        ordering = ['-created_at']
        indexes = [models.Index(fields=['child', 'status'])]

    def __str__(self):
        return f"מזומן {self.child.full_name} - ₪{self.total_amount}"


class CashPlanMonth(models.Model):
    """One month of a cash plan, and the document issued for it."""
    STATUS_CHOICES = [
        ('pending', 'ממתין'),
        ('invoiced', 'הופקה חשבונית'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        CashPlan, on_delete=models.CASCADE, related_name='months', verbose_name="תוכנית",
    )
    due_date = models.DateField(verbose_name="תאריך המסמך")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="סכום")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="סטטוס")
    document = models.ForeignKey(
        FormalDocument, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cash_plan_month', verbose_name="מסמך",
    )
    invoiced_at = models.DateTimeField(null=True, blank=True, verbose_name="מועד ההפקה")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'cash_plan_months'
        verbose_name = "חודש במנוי מזומן"
        verbose_name_plural = "חודשים במנוי מזומן"
        ordering = ['due_date']
        # One document per month per plan: the cron and beat can overlap.
        unique_together = [('plan', 'due_date')]
        indexes = [models.Index(fields=['status', 'due_date'])]

    def __str__(self):
        return f"{self.plan.child.full_name} · {self.due_date:%m/%Y} · ₪{self.amount}"


class CheckPlan(models.Model):
    """Office check series for a child who cannot pay by card."""

    STATUS_CHOICES = [
        ('active', 'פעיל'),
        ('completed', 'הושלם'),
        ('cancelled', 'בוטל'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey(
        'customers.Child',
        on_delete=models.CASCADE,
        related_name='check_plans',
        verbose_name="ילד",
    )
    lesson = models.ForeignKey(
        'courses.Lesson',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='check_plans',
        verbose_name="שיעור",
    )
    description = models.CharField(max_length=300, blank=True, verbose_name="תיאור")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', verbose_name="סטטוס")
    receipt = models.ForeignKey(
        FormalDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='check_plan_receipts',
        verbose_name="קבלה",
    )
    branch = models.ForeignKey(
        'core.Branch',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='check_plans',
        verbose_name="סניף",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'check_plans'
        verbose_name = "תוכנית צ'קים"
        verbose_name_plural = "תוכניות צ'קים"
        ordering = ['-created_at']

    def __str__(self):
        return f"צ'קים {self.child_id} — {self.get_status_display()}"


class CheckItem(models.Model):
    """One post-dated check in a CheckPlan."""

    STATUS_CHOICES = [
        ('pending', 'ממתין'),
        ('invoiced', 'הופקה חשבונית'),
        ('cancelled', 'בוטל'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        CheckPlan,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name="תוכנית",
    )
    due_date = models.DateField(verbose_name="תאריך צ'ק")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="סכום")
    bank = models.CharField(max_length=100, blank=True, verbose_name="בנק")
    bank_branch = models.CharField(max_length=50, blank=True, verbose_name="סניף בנק")
    account_number = models.CharField(max_length=50, blank=True, verbose_name="מספר חשבון")
    check_number = models.CharField(max_length=50, blank=True, verbose_name="מספר צ'ק")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="סטטוס")
    tax_invoice = models.ForeignKey(
        FormalDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='check_item_invoices',
        verbose_name="חשבונית מס",
    )
    invoiced_at = models.DateTimeField(null=True, blank=True, verbose_name="תאריך הפקת חשבונית")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'check_items'
        verbose_name = "צ'ק"
        verbose_name_plural = "צ'קים"
        ordering = ['due_date', 'created_at']

    def __str__(self):
        return f"צ'ק {self.check_number or self.id} ₪{self.amount}"
