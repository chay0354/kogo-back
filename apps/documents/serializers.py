from rest_framework import serializers
from apps.documents.models import CashPlan, CashPlanMonth, FormalDocument, DocumentLineItem, DocumentPayment, CheckPlan, CheckItem


class DocumentLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentLineItem
        fields = ['id', 'sku', 'description', 'quantity', 'unit_price', 'line_total']
        read_only_fields = ['id', 'line_total']


class DocumentPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentPayment
        fields = [
            'id', 'payment_method', 'amount', 'reference', 'notes',
            'check_date', 'check_bank', 'check_branch', 'check_account', 'check_crossed',
            'card_last_four', 'card_expiry', 'card_installments',
        ]
        read_only_fields = ['id']


def _allocation_required(obj) -> bool:
    """A tax invoice above the threshold to a business customer — never a credit note or a family."""
    from apps.documents.invoice_document import document_needs_allocation

    return document_needs_allocation(
        obj.document_type, obj.subtotal - obj.discount_amount, to_business=obj.client_type == 'business',
    )


class FormalDocumentSerializer(serializers.ModelSerializer):
    line_items = DocumentLineItemSerializer(many=True, read_only=True)
    payments = DocumentPaymentSerializer(many=True, read_only=True)
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)

    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    allocation_required = serializers.SerializerMethodField()

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display', 'draft_target_type',
            'client_type', 'child', 'business_customer',
            'business', 'business_name', 'business_category', 'business_category_name',
            'document_date', 'due_date', 'description', 'currency',
            'prices_include_vat', 'payment_terms',
            'vat_exempt', 'vat_percent',
            'subtotal', 'discount_amount', 'discount_percent', 'vat_amount', 'total_amount',
            'customer_notes', 'internal_notes',
            'linked_document', 'linked_document_number', 'credit_reason',
            'tranzila_doc_id', 'pdf_url', 'tranzila_issued',
            'allocation_number', 'allocation_required', 'allocation_entered_at',
            'branch', 'created_at', 'updated_at',
            'line_items', 'payments',
        ]
        read_only_fields = ['id', 'document_number', 'created_at', 'updated_at']

    def get_allocation_required(self, obj):
        return _allocation_required(obj)



class FormalDocumentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list/dropdown views."""
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    customer_name = serializers.SerializerMethodField()

    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    allocation_required = serializers.SerializerMethodField()

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display',
            'document_date', 'total_amount', 'currency', 'tranzila_issued', 'pdf_url',
            'customer_name', 'tranzila_doc_id',
            'business_name', 'business_category_name',
            'allocation_number', 'allocation_required',
        ]

    def get_allocation_required(self, obj):
        return _allocation_required(obj)

    def get_customer_name(self, obj):
        if obj.child_id:
            return obj.child.full_name
        if obj.business_customer_id:
            return obj.business_customer.full_name
        return ''


# ── Write serializers ────────────────────────────────────────────────────────

class LineItemInputSerializer(serializers.Serializer):
    sku = serializers.CharField(required=False, allow_blank=True, default='')
    description = serializers.CharField(required=False, allow_blank=True, default='')
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, default=1)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)


class InvoiceDetailsInputSerializer(serializers.Serializer):
    document_date = serializers.DateField()
    due_date = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    currency = serializers.ChoiceField(choices=['ILS', 'USD', 'EUR'], default='ILS')
    prices_include_vat = serializers.BooleanField(default=False)
    line_items = LineItemInputSerializer(many=True)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, default=0)
    vat_exempt = serializers.BooleanField(default=False)
    round_total = serializers.BooleanField(default=False)
    payment_terms = serializers.CharField(required=False, allow_blank=True, default='')
    customer_notes = serializers.CharField(required=False, allow_blank=True, default='')
    internal_notes = serializers.CharField(required=False, allow_blank=True, default='')
    payment_methods = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    # A combined document paid by check: the check is crossed "לא סחיר", in the
    # customer's name (הוראה 18ב(ד)(2)). A receipt says it per check, in
    # receipt_details.checks[].check_crossed.
    check_crossed = serializers.BooleanField(required=False, default=False)


class ReceiptDetailsInputSerializer(serializers.Serializer):
    payment_method = serializers.CharField()
    linked_invoice_id = serializers.CharField(required=False, allow_blank=True, default='')
    cash_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_notes = serializers.CharField(required=False, allow_blank=True, default='')
    checks = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    withholding = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)
    check_notes = serializers.CharField(required=False, allow_blank=True, default='')
    card_last_four = serializers.CharField(required=False, allow_blank=True, default='')
    card_expiry = serializers.CharField(required=False, allow_blank=True, default='')
    card_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)
    card_installments = serializers.IntegerField(default=1)
    card_notes = serializers.CharField(required=False, allow_blank=True, default='')
    bank_date = serializers.DateField(required=False, allow_null=True)
    bank_reference = serializers.CharField(required=False, allow_blank=True, default='')
    bank_amount = serializers.DecimalField(max_digits=12, decimal_places=2, default=0)
    bank_notes = serializers.CharField(required=False, allow_blank=True, default='')


class CreditInvoiceInputSerializer(serializers.Serializer):
    document_date = serializers.DateField()
    # A credit note names the document it credits (its number and date): the
    # dialog always asked for it, and now the API does too. The number may be
    # one kogo never issued (the previous software's), so it is not looked up
    # here; its date is found when kogo has the document, or given with it.
    linked_invoice_id = serializers.CharField(
        max_length=30,
        error_messages={
            'required': 'חשבונית זיכוי חייבת לציין את מספר המסמך המקורי',
            'blank': 'חשבונית זיכוי חייבת לציין את מספר המסמך המקורי',
        },
    )
    linked_document_date = serializers.DateField(required=False, allow_null=True)
    credit_reason = serializers.CharField()
    credit_amount_before_vat = serializers.DecimalField(max_digits=12, decimal_places=2)
    vat_exempt = serializers.BooleanField(default=False)
    customer_notes = serializers.CharField(required=False, allow_blank=True, default='')
    internal_notes = serializers.CharField(required=False, allow_blank=True, default='')


class CreateDocumentSerializer(serializers.Serializer):
    """Top-level create payload for all document types."""
    document_type = serializers.ChoiceField(choices=[
        'tax_invoice', 'receipt', 'combined', 'transaction_invoice', 'credit_invoice', 'draft'
    ])
    # Income tagging; defaults to the business customer's own when omitted.
    business_id = serializers.UUIDField(required=False, allow_null=True)
    business_category_id = serializers.UUIDField(required=False, allow_null=True)
    # Only for drafts: what the document becomes when approved.
    draft_target_type = serializers.ChoiceField(
        choices=['tax_invoice', 'transaction_invoice'], required=False, allow_blank=True,
    )
    client_type = serializers.ChoiceField(choices=['business', 'existing'])
    child_id = serializers.UUIDField(required=False, allow_null=True)
    business_customer_id = serializers.UUIDField(required=False, allow_null=True)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    document_date = serializers.DateField(required=False)
    # A combined document (חשבונית מס/קבלה) sends only its payment methods'
    # names, no check lines: this says the check it was paid with is crossed
    # "לא סחיר" in the customer's name (הוראה 18ב(ד)(2)), for every check row
    # it creates. Absent or false → the signed original goes on paper.
    check_crossed = serializers.BooleanField(required=False, default=False)

    invoice_details = InvoiceDetailsInputSerializer(required=False)
    receipt_details = ReceiptDetailsInputSerializer(required=False)
    credit_invoice_details = CreditInvoiceInputSerializer(required=False)

    # Which section each type is built from. Optional above because a receipt
    # carries no invoice section and an invoice carries no receipt one.
    _SECTIONS_BY_TYPE = {
        'tax_invoice': ('invoice_details',),
        'transaction_invoice': ('invoice_details',),
        'draft': ('invoice_details',),
        # A combined document is built from the invoice section alone; how it
        # was paid comes from invoice_details.payment_methods.
        'combined': ('invoice_details',),
        'receipt': ('receipt_details',),
        'credit_invoice': ('credit_invoice_details',),
    }
    _SECTION_LABELS = {
        'invoice_details': 'פרטי החשבונית',
        'receipt_details': 'פרטי הקבלה',
        'credit_invoice_details': 'פרטי הזיכוי',
    }

    def validate(self, attrs):
        """Say which section is missing, instead of failing deep inside the service."""
        missing = {
            section: [f'{self._SECTION_LABELS[section]} חסרים למסמך מסוג זה']
            for section in self._SECTIONS_BY_TYPE.get(attrs.get('document_type'), ())
            if section not in attrs
        }
        if missing:
            raise serializers.ValidationError(missing)
        return attrs


class CheckItemSerializer(serializers.ModelSerializer):
    tax_invoice_number = serializers.CharField(source='tax_invoice.document_number', read_only=True, allow_null=True)

    class Meta:
        model = CheckItem
        fields = [
            'id', 'due_date', 'amount', 'bank', 'bank_branch', 'account_number',
            'check_number', 'status', 'tax_invoice', 'tax_invoice_number', 'invoiced_at',
        ]
        read_only_fields = fields


class CheckPlanSerializer(serializers.ModelSerializer):
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    lesson_name = serializers.SerializerMethodField()
    receipt_number = serializers.CharField(source='receipt.document_number', read_only=True, allow_null=True)
    items = CheckItemSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()
    next_due_date = serializers.SerializerMethodField()

    class Meta:
        model = CheckPlan
        fields = [
            'id', 'child', 'child_name', 'lesson', 'lesson_name', 'description',
            'status', 'receipt', 'receipt_number', 'branch', 'branch_name',
            'items', 'total_amount', 'next_due_date', 'created_at',
        ]
        read_only_fields = fields

    def get_lesson_name(self, obj):
        if obj.lesson_id and obj.lesson:
            course = getattr(obj.lesson, 'course', None)
            return course.name if course else str(obj.lesson)
        return None

    def get_total_amount(self, obj):
        return sum((item.amount for item in obj.items.all()), start=0)

    def get_next_due_date(self, obj):
        pending = [item.due_date for item in obj.items.all() if item.status == 'pending']
        return min(pending) if pending else None

    def to_representation(self, obj):
        # Business → city → course type → age → instructor, as on every ledger
        # row, so the invoices page's filter bar narrows plans like charges.
        # branch_id is the plan's own branch: its lesson's, or the family's when
        # it has no lesson (register_check_plan).
        from apps.core.ledger_dimensions import row_dimensions
        return {
            **super().to_representation(obj),
            **row_dimensions(lesson=obj.lesson, branch=obj.branch),
            'branch_id': str(obj.branch_id) if obj.branch_id else None,
        }


class CreateCheckPlanSerializer(serializers.Serializer):
    child_id = serializers.UUIDField()
    lesson_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    checks = serializers.ListField(child=serializers.DictField(), allow_empty=False)


# ── Cash plans ───────────────────────────────────────────────────────────────

class CashPlanMonthSerializer(serializers.ModelSerializer):
    document_number = serializers.CharField(source='document.document_number', read_only=True, default='')
    document_type = serializers.CharField(source='document.document_type', read_only=True, default='')

    class Meta:
        model = CashPlanMonth
        fields = ['id', 'due_date', 'amount', 'status', 'invoiced_at', 'document_number', 'document_type']


class CashPlanSerializer(serializers.ModelSerializer):
    child_name = serializers.CharField(source='child.full_name', read_only=True, default='')
    course_name = serializers.CharField(source='lesson.course.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')
    receipt_number = serializers.CharField(source='receipt.document_number', read_only=True, default='')
    months = CashPlanMonthSerializer(many=True, read_only=True)
    months_paid = serializers.SerializerMethodField()
    months_total = serializers.SerializerMethodField()

    class Meta:
        model = CashPlan
        fields = [
            'id', 'child', 'child_name', 'lesson', 'course_name', 'branch', 'branch_name',
            'description', 'status', 'total_amount', 'monthly_amount',
            'monthly_document_type', 'receipt', 'receipt_number',
            'months', 'months_paid', 'months_total', 'created_at',
        ]

    def get_months_paid(self, obj):
        return sum(1 for m in obj.months.all() if m.status == 'invoiced')

    def get_months_total(self, obj):
        return obj.months.count()


class CreateCashPlanSerializer(serializers.Serializer):
    child_id = serializers.UUIDField()
    lesson_id = serializers.UUIDField(required=False, allow_null=True)
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    monthly_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    start_month = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    monthly_document_type = serializers.ChoiceField(
        choices=[c[0] for c in CashPlan.MONTHLY_DOCUMENT_CHOICES],
        required=False,
        default='combined',
    )
