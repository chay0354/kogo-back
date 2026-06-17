import uuid
from django.db import models
from django.db import transaction


DOCUMENT_TYPE_CHOICES = [
    ('tax_invoice', 'חשבונית מס'),
    ('receipt', 'קבלה'),
    ('combined', 'חשבונית מס/קבלה'),
    ('transaction_invoice', 'חשבונית עסקה'),
    ('credit_invoice', 'חשבונית מס זיכוי'),
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

    # Tranzila document data
    tranzila_doc_id = models.CharField(max_length=100, blank=True, verbose_name="מזהה מסמך טרנזילה")
    tranzila_retrieval_key = models.CharField(max_length=100, blank=True, verbose_name="מפתח אחזור טרנזילה")
    pdf_url = models.URLField(blank=True, verbose_name="קישור PDF")
    tranzila_issued = models.BooleanField(default=False, verbose_name="הופק בטרנזילה")

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
