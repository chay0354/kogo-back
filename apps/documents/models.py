import hashlib
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
    # The last number handed out; start - 1 while the run has handed out none.
    counter = models.PositiveIntegerField(default=0)
    # The run's first number. 1, unless the run continues the previous
    # software's run of the same type (DocumentSeriesOpening says from where).
    start = models.PositiveIntegerField(default=1, verbose_name="מספר פתיחה")

    class Meta:
        db_table = 'document_series'
        unique_together = [('series', 'year')]

    def __str__(self) -> str:
        return f'{self.series}-{self.year}: {self.counter}'

    @property
    def issued(self) -> int:
        """How many numbers the run handed out: start .. counter."""
        return max(0, self.counter - self.start + 1)

    @classmethod
    def next_number(cls, series: str, year: int) -> int:
        """The next number, under a row lock; a caller that rolls back gives it back."""
        with transaction.atomic():
            row, _ = cls.objects.select_for_update().get_or_create(
                series=series, year=year, defaults={'counter': 0},
            )
            # A run that starts above 1 hands out its start first.
            row.counter = max(row.counter, row.start - 1) + 1
            row.save(update_fields=['counter'])
            return row.counter

    @classmethod
    def open_at(cls, series: str, year: int, start: int) -> 'DocumentSeries':
        """
        Make `start` the next number of a run that has handed out nothing yet.

        Under the same row lock next_number takes, so no number can be handed
        out between the check and the change. A run that handed out even one
        number is refused: an issued number is never renumbered (סעיף 23(ב)),
        and a run cannot start below a number it already gave. The caller runs
        this inside its own transaction, with the audit record beside it.
        """
        if start < 1:
            raise ValueError('A run starts at 1 or above')
        row, _ = cls.objects.select_for_update().get_or_create(
            series=series, year=year, defaults={'counter': 0},
        )
        if row.counter != 0:
            raise SeriesAlreadyIssued(row)
        row.start = start
        row.counter = start - 1
        row.save(update_fields=['start', 'counter'])
        return row


class SeriesAlreadyIssued(Exception):
    """The run handed out numbers (or was opened) already, so its start is fixed."""

    def __init__(self, row: DocumentSeries):
        super().__init__(f'{row.series}-{row.year} is at {row.counter}')
        self.row = row


class DocumentSeriesOpening(models.Model):
    """
    The record of a run that continues the previous software's run of its type.

    The business moved to kogo from another invoicing program, which numbered
    each document type in a run of its own that never reset. So the books stay
    one consecutive run, a kogo run may start where the old one stopped: the
    old program's last number, plus one. This keeps who set it, when, from what
    and why. It is written once, beside the run's change, and never edited or
    deleted — a mistake is not fixed by rewriting history.

    One old run is continued by one kogo run a tax year: two kogo runs that
    both start at 121883 would be two runs of one type sharing numbers.
    """
    series = models.CharField(max_length=10, verbose_name="סדרה")
    year = models.PositiveIntegerField(verbose_name="שנת מס")
    start = models.PositiveIntegerField(verbose_name="המספר הראשון בסדרה")
    previous_last_number = models.PositiveIntegerField(verbose_name="המספר האחרון בתוכנה הקודמת")
    previous_type_label = models.CharField(max_length=50, verbose_name="סוג המסמך בתוכנה הקודמת")
    note = models.TextField(blank=True, verbose_name="מקור / סיבה")
    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='series_openings', verbose_name="מי פתח",
    )
    # The name as it was, so the record still says who once the account is gone.
    created_by_name = models.CharField(max_length=150, blank=True, verbose_name="שם הפותח")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="מתי")

    class Meta:
        db_table = 'document_series_openings'
        ordering = ['year', 'series']
        constraints = [
            models.UniqueConstraint(fields=['series', 'year'], name='series_opening_once_per_run'),
            models.UniqueConstraint(
                fields=['year', 'previous_type_label'], name='series_opening_one_run_per_old_run',
            ),
            models.CheckConstraint(
                check=models.Q(start=models.F('previous_last_number') + 1),
                name='series_opening_continues_the_old_run',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.series}-{self.year} from {self.start} ({self.previous_type_label} {self.previous_last_number})'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('A series opening is never edited')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('A series opening is never deleted')


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
    # הוראה 18ב(ד)(2): a document signed with a secured (not approved) signature
    # may be emailed for a check only when the check is crossed "לא סחיר", in the
    # customer's name, to the business's order. Nothing on a check row said so,
    # so an unmarked check sends its receipt's original on paper. Nullable so the
    # column can land (Vercel migrates at build time) while the previous code,
    # which inserts payments without it, is still serving; NULL reads as not crossed.
    check_crossed = models.BooleanField(
        null=True, blank=True, default=False, verbose_name="צ'ק משורטט לא סחיר על שם הלקוח",
    )

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


# What a signed original is, once signed: never re-signed, never rewritten. Its
# purpose is part of it — an archive copy never becomes an original, which the
# office could then print or mail as "מקור".
SIGNED_ORIGINAL_FROZEN_FIELDS = ('pdf', 'sha256', 'size', 'key_id', 'cert_fingerprint', 'signed_at', 'purpose')


class FrozenSignedOriginalError(Exception):
    """An attempt to change or delete the signed bytes of an issued document."""


class SignedOriginalQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # A bulk update skips save(); it may sign a row that is not yet signed
        # (nothing does that today, but the rule is the same), never re-sign one.
        frozen = sorted(name for name in kwargs if name in SIGNED_ORIGINAL_FROZEN_FIELDS)
        if frozen and self.filter(signed_at__isnull=False).exists():
            raise FrozenSignedOriginalError(
                f'{", ".join(frozen)} of a signed original never change once it is signed'
            )
        return super().update(**kwargs)

    def delete(self):
        raise FrozenSignedOriginalError('A signed original is kept for seven years and never deleted')


class SignedOriginal(models.Model):
    """
    The original of an issued fiscal document, signed and kept as it was signed.

    הוראה 1 להוראות ניהול פנקסי חשבונות: a document sent by computer is a
    "מסמך ממוחשב" only when it carries an approved or secured electronic
    signature; חוזר 24/2004 asks for it to be kept "בצורתו המקורית, כולל
    החתימה" for seven years. Until this table every mail and every download
    drew the PDF again, and reportlab stamps the moment it draws, so no two
    renders were the same file and none was kept. Now the original is drawn
    once, at issue, signed (apps/documents/signing), stored here as bytes with
    its SHA-256 — the rental contracts' precedent (apps/rentals/models.py) —
    and every email attaches exactly these bytes. A later print is a copy.

    bytea and not storage: the originals then ride in the quarterly database
    backup that סעיף 25(ו)(2) already requires, with nothing else to keep.

    One row per document number, across every run kogo numbers (IR lesson
    receipts, ST/SD store sales, and the FormalDocument runs — TI, IRM, RC,
    TX, CR, RT). The row is written inside the transaction that issues the
    document, before anything is signed, so a document issued while signing is
    on is never left without one; the signature follows after the commit, or
    from the sign-pending cron when the key was out of reach.
    """
    KIND_IR = 'ir'
    KIND_STORE = 'store'
    KIND_FORMAL = 'formal'
    KIND_CHOICES = [
        (KIND_IR, 'קבלת חוג'),
        (KIND_STORE, 'מכירת חנות'),
        (KIND_FORMAL, 'מסמך'),
    ]

    DELIVERY_EMAIL = 'email'
    DELIVERY_PAPER = 'paper'
    DELIVERY_HELD = 'held'
    DELIVERY_NONE = 'none'
    DELIVERY_CHOICES = [
        (DELIVERY_EMAIL, 'במייל'),
        (DELIVERY_PAPER, 'למסירה על נייר'),
        (DELIVERY_HELD, 'ממתין'),
        (DELIVERY_NONE, 'לא נשלח'),
    ]

    # How kogo itself sends the document, when it does; '' for a document that
    # only goes into the archive (a hand-issued invoice, a till sale).
    CHANNEL_IR = 'ir'
    CHANNEL_STORE = 'store'
    CHANNEL_CREDIT_NOTE = 'credit_note'
    CHANNEL_RENTAL = 'rental'
    CHANNEL_CHOICES = [
        ('', 'לא נשלח על ידי המערכת'),
        (CHANNEL_IR, 'מייל קבלת חוג'),
        (CHANNEL_STORE, 'מייל חנות האתר'),
        (CHANNEL_CREDIT_NOTE, 'מייל הודעת זיכוי'),
        (CHANNEL_RENTAL, 'מייל קבלת שכירות'),
    ]

    # What the stored file is. An original is the one "מקור", signed at issue.
    # An archive copy is a document kogo issued before signing existed, drawn
    # again from its record and signed only to be kept (apps/documents/signing/
    # archive.py): its customer already holds the original, and תקנה 9א(א)(2) /
    # הוראה 18(ב)(2) forbid producing "מקור" twice, so the copy says "העתק
    # לארכיון" on its face and is never mailed, printed as an original or put
    # on the hand-delivery list.
    PURPOSE_ORIGINAL = 'original'
    PURPOSE_ARCHIVE = 'archive'
    PURPOSE_CHOICES = [
        (PURPOSE_ORIGINAL, 'מקור'),
        (PURPOSE_ARCHIVE, 'העתק לארכיון'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=50, unique=True, verbose_name="מספר מסמך")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, verbose_name="סוג מקור")
    # The id of the issuing row: Invoice (ir), StoreInvoice (store), FormalDocument (formal).
    source_id = models.CharField(max_length=64, verbose_name="מזהה המקור")
    channel = models.CharField(
        max_length=20, choices=CHANNEL_CHOICES, blank=True, default='', verbose_name="ערוץ שליחה",
    )
    # NULL is an original. The column is nullable for the same reason as
    # document_payments.check_crossed (0011): Vercel migrates the production
    # database while the previous code still serves, and that code inserts
    # originals without this column. Django 4.2 has no db_default, and its
    # default is written by Django, not by the database — so every reader asks
    # "is it an archive copy?" (purpose == 'archive'), never "is it 'original'?".
    purpose = models.CharField(
        max_length=10, choices=PURPOSE_CHOICES, default=PURPOSE_ORIGINAL, null=True, blank=True,
        db_index=True, verbose_name="מהות הקובץ",
    )

    # The signed file itself, NULL until it is signed.
    pdf = models.BinaryField(null=True, blank=True, verbose_name="המקור החתום (PDF)")
    sha256 = models.CharField(max_length=64, blank=True, default='', verbose_name="SHA-256 של הקובץ")
    size = models.PositiveIntegerField(default=0, verbose_name="גודל בבתים")
    key_id = models.CharField(max_length=300, blank=True, default='', verbose_name="מפתח החתימה")
    cert_fingerprint = models.CharField(max_length=64, blank=True, default='', verbose_name="טביעת אצבע של התעודה")
    signed_at = models.DateTimeField(null=True, blank=True, verbose_name="מועד החתימה")
    sign_attempts = models.PositiveSmallIntegerField(default=0, verbose_name="ניסיונות חתימה")

    delivery = models.CharField(
        max_length=10, choices=DELIVERY_CHOICES, default=DELIVERY_HELD, verbose_name="מסירה",
    )
    # Written for the office, in Hebrew: why the original goes where it goes.
    delivery_reason = models.CharField(max_length=300, blank=True, default='', verbose_name="סיבה")

    # What the office list shows, as the document said it when it was issued.
    document_type_label = models.CharField(max_length=50, blank=True, default='', verbose_name="סוג מסמך")
    customer_name = models.CharField(max_length=200, blank=True, default='', verbose_name="שם הלקוח")
    document_date = models.DateField(null=True, blank=True, verbose_name="תאריך המסמך")
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name='סה"כ')
    # Where the mail was meant to go when the document was issued — a refund's
    # credit note is addressed by its caller, and the cron sends it later.
    email_to = models.CharField(max_length=254, blank=True, default='', verbose_name="נשלח אל")

    # Claimed before the mail goes out and cleared when it fails, so two
    # senders — the issuing request and the cron — never both send it.
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="נשלח במייל")
    send_attempts = models.PositiveSmallIntegerField(default=0, verbose_name="ניסיונות שליחה")
    last_error = models.CharField(max_length=300, blank=True, default='', verbose_name="שגיאה אחרונה")

    # נספח ה'(א)(4): "מקור" once. The stored original handed over on paper,
    # printed at most one time; every later print is a copy.
    paper_original_printed_at = models.DateTimeField(null=True, blank=True, verbose_name="המקור הודפס")
    paper_original_printed_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name="מי הדפיס את המקור",
    )

    # The copy in the locked backup bucket (signing/backup.py): when it landed,
    # or why the last try did not. Not frozen — a signed row is backed up after.
    backup_at = models.DateTimeField(null=True, blank=True, verbose_name="גובה לאחסון הנעול")
    # Nullable: the code already deployed inserts rows without it while Vercel migrates.
    backup_error = models.CharField(max_length=300, blank=True, null=True, default='', verbose_name="שגיאת גיבוי אחרונה")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="נוצר")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="עודכן")

    objects = SignedOriginalQuerySet.as_manager()

    class Meta:
        db_table = 'signed_originals'
        verbose_name = "מקור חתום"
        verbose_name_plural = "מקורות חתומים"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['kind', 'source_id'], name='signed_orig_source_idx'),
            models.Index(fields=['delivery', 'paper_original_printed_at'], name='signed_orig_delivery_idx'),
            models.Index(fields=['signed_at'], name='signed_orig_signed_at_idx'),
        ]
        constraints = [
            # A signed row carries its bytes and their fingerprint; an unsigned one neither.
            models.CheckConstraint(
                check=(
                    models.Q(signed_at__isnull=True, pdf__isnull=True)
                    | (models.Q(signed_at__isnull=False, pdf__isnull=False, size__gt=0) & ~models.Q(sha256=''))
                ),
                name='signed_original_bytes_with_signature',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.number} ({self.get_delivery_display()})'

    @property
    def is_signed(self) -> bool:
        return self.signed_at is not None

    @property
    def is_archive_copy(self) -> bool:
        return self.purpose == self.PURPOSE_ARCHIVE

    def pdf_intact(self) -> bool:
        """The stored bytes still hash to the fingerprint taken when they were signed."""
        if self.pdf is None or not self.sha256:
            return False
        return hashlib.sha256(bytes(self.pdf)).hexdigest() == self.sha256

    def save(self, *args, **kwargs):
        # Signed once, never again: a save may sign an unsigned row, and may
        # move its delivery, but never touches the bytes of one already signed.
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).values(*SIGNED_ORIGINAL_FROZEN_FIELDS[1:]).first()
            if stored and stored['signed_at'] is not None:
                update_fields = kwargs.get('update_fields')
                touched = set(SIGNED_ORIGINAL_FROZEN_FIELDS) if update_fields is None else set(update_fields)
                changed = [name for name in touched & set(stored) if getattr(self, name) != stored[name]]
                if 'pdf' in touched and not self.pdf_intact():
                    changed.append('pdf')
                if changed:
                    raise FrozenSignedOriginalError(
                        f'{self.number} is signed; {", ".join(sorted(changed))} never change'
                    )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise FrozenSignedOriginalError('A signed original is kept for seven years and never deleted')


class SignedFileAccess(models.Model):
    """
    One time a stored signed file left kogo: who took it, when, how, and from where.

    The stored files are the business's fiscal record — the originals and the
    archive copies (SignedOriginal). Handing one out is logged before the bytes
    go, so the log never misses a file that left; a download that fails after
    the log is an extra line, never a missing one. Kept like the files it
    points at: the original is PROTECTed from deletion by its own log.
    """
    ACTION_DOWNLOAD = 'download'
    ACTION_EXPORT = 'export'
    ACTION_CHOICES = [
        (ACTION_DOWNLOAD, 'הורדה'),
        (ACTION_EXPORT, 'ייצוא'),
    ]

    original = models.ForeignKey(
        SignedOriginal, on_delete=models.PROTECT, related_name='accesses', verbose_name="הקובץ החתום",
    )
    user = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name="משתמש",
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES, verbose_name="פעולה")
    at = models.DateTimeField(auto_now_add=True, verbose_name="מתי")
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="כתובת IP")

    class Meta:
        db_table = 'signed_file_access'
        verbose_name = "גישה לקובץ חתום"
        verbose_name_plural = "גישות לקבצים חתומים"
        ordering = ['-at']

    def __str__(self) -> str:
        return f'{self.original_id} {self.action} {self.at:%Y-%m-%d %H:%M}'
