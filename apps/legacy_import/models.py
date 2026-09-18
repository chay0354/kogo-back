"""The documents the previous software issued, kept as history — never as kogo's own.

A LegacyDocument was issued by the old management/invoicing software, under its
numbers, before the business moved to kogo. It is here so that looking at a
customer shows what was issued to them and what the last numbers were. It is
not a FormalDocument, not in any of kogo's number runs, and not income kogo
reports: the register, the period report, the uniform-format export, the
numbering continuity check and the dashboard each read their own tables
(FormalDocument, customers.Invoice, StoreInvoice, Payment) by name, and this
table is none of them. It lives in an app of its own for the same reason: a
document kogo issues and a document it only remembers must never be one query
apart.
"""
import uuid

from django.conf import settings
from django.db import models

LEGACY_DOCUMENT_TYPE_CHOICES = [
    ('combined', 'חשבונית מס/קבלה'),
    ('tax_invoice', 'חשבונית מס'),
    ('receipt', 'קבלה'),
    ('transaction_invoice', 'חשבונית עסקה'),
    ('credit_invoice', 'חשבונית מס זיכוי'),
]


class LegacyImport(models.Model):
    """One file uploaded from the old software: previewed, then committed."""

    STATUS_PREVIEW = 'preview'
    STATUS_COMMITTED = 'committed'
    STATUS_CHOICES = [
        (STATUS_PREVIEW, 'תצוגה מקדימה'),
        (STATUS_COMMITTED, 'יובא'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file_name = models.CharField(max_length=255, verbose_name="שם הקובץ")
    sha256 = models.CharField(max_length=64, db_index=True, verbose_name="טביעת הקובץ")
    row_count = models.PositiveIntegerField(default=0, verbose_name="מספר שורות")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PREVIEW, verbose_name="סטטוס")
    # The rows as parser.parse_sheet normalised them. Only the columns the
    # parser reads are here: never the customer's app password, never a birth date.
    rows = models.JSONField(default=list, blank=True, verbose_name="שורות")
    # What the preview showed, so reopening it does not re-read the file.
    summary = models.JSONField(default=dict, blank=True, verbose_name="סיכום")
    # location -> {business_id, category_id, branch_id}, as the owner confirmed it.
    mapping = models.JSONField(default=dict, blank=True, verbose_name="מיפוי מיקומים")
    include_subscription_parents = models.BooleanField(default=False, verbose_name="כולל הורים משלמי מנוי")
    result = models.JSONField(default=dict, blank=True, verbose_name="תוצאת הייבוא")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='legacy_imports', verbose_name="הועלה על ידי",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="מועד ההעלאה")
    committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='legacy_imports_committed', verbose_name="יובא על ידי",
    )
    committed_at = models.DateTimeField(null=True, blank=True, verbose_name="מועד הייבוא")

    class Meta:
        db_table = 'legacy_imports'
        verbose_name = "ייבוא מהתוכנה הקודמת"
        verbose_name_plural = "ייבואים מהתוכנה הקודמת"
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.file_name} ({self.get_status_display()})'


class LegacyDocument(models.Model):
    """
    One document issued by the previous software — read-only history.

    `number` is the old software's number, in the old software's run for its
    type; UNIQUE(doc_type, number) is what makes importing the same file twice
    update the rows instead of adding them again.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_import = models.ForeignKey(
        LegacyImport, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='documents', verbose_name="הייבוא האחרון שכתב אותו",
    )

    original_type = models.CharField(max_length=60, verbose_name="סוג המסמך בתוכנה הקודמת")
    doc_type = models.CharField(max_length=30, choices=LEGACY_DOCUMENT_TYPE_CHOICES, verbose_name="סוג המסמך")
    number = models.PositiveBigIntegerField(verbose_name="מספר המסמך בתוכנה הקודמת")
    document_date = models.DateField(verbose_name="תאריך")

    invoice_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='סה"כ חשבונית')
    receipt_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='סה"כ קבלה')
    credit_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='סה"כ זיכוי')
    withholding_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="סכום הניכוי")
    total_before_withholding = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name='סה"כ לפני ניכוי',
    )

    original_status = models.CharField(max_length=60, blank=True, verbose_name="סטטוס בתוכנה הקודמת")
    payment_type = models.CharField(max_length=60, blank=True, verbose_name="סוג תשלום")
    card_last_four = models.CharField(max_length=4, blank=True, verbose_name="4 ספרות כרטיס")
    location = models.CharField(max_length=300, blank=True, verbose_name="מיקום בתוכנה הקודמת")
    details = models.TextField(blank=True, verbose_name="פרטים")
    remark = models.TextField(blank=True, verbose_name="הערה")

    # Who it was issued to, as the old software had them on this document.
    customer_key = models.CharField(max_length=120, blank=True, db_index=True, verbose_name="מפתח לקוח")
    customer_name = models.CharField(max_length=300, blank=True, verbose_name="שם הלקוח")
    customer_email = models.CharField(max_length=254, blank=True, verbose_name="אימייל")
    customer_phone = models.CharField(max_length=30, blank=True, verbose_name="טלפון")
    business_customer = models.ForeignKey(
        'customers.BusinessCustomer', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='legacy_documents', verbose_name="לקוח עסקי",
    )

    business = models.ForeignKey(
        'core.Business', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='legacy_documents', verbose_name="עסק",
    )
    business_category = models.ForeignKey(
        'core.BusinessCategory', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='legacy_documents', verbose_name="קטגוריה בעסק",
    )
    branch = models.ForeignKey(
        'core.Branch', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='legacy_documents', verbose_name="סניף",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'legacy_documents'
        verbose_name = "מסמך מהתוכנה הקודמת"
        verbose_name_plural = "מסמכים מהתוכנה הקודמת"
        ordering = ['-document_date', '-number']
        constraints = [
            models.UniqueConstraint(fields=['doc_type', 'number'], name='legacy_document_type_number_unique'),
        ]
        indexes = [
            models.Index(fields=['business_customer', '-document_date'], name='legacy_doc_customer_date'),
        ]

    def __str__(self):
        return f'{self.original_type} {self.number} (תוכנה קודמת)'
