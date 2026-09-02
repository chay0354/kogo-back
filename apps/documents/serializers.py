from rest_framework import serializers
from apps.documents.models import FormalDocument, DocumentLineItem, DocumentPayment, CheckPlan, CheckItem


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
            'check_date', 'check_bank', 'check_branch', 'check_account',
            'card_last_four', 'card_expiry', 'card_installments',
        ]
        read_only_fields = ['id']


class FormalDocumentSerializer(serializers.ModelSerializer):
    line_items = DocumentLineItemSerializer(many=True, read_only=True)
    payments = DocumentPaymentSerializer(many=True, read_only=True)
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)

    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
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
            'branch', 'created_at', 'updated_at',
            'line_items', 'payments',
        ]
        read_only_fields = ['id', 'document_number', 'created_at', 'updated_at']


class FormalDocumentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list/dropdown views."""
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display',
            'document_date', 'total_amount', 'currency', 'tranzila_issued', 'pdf_url',
            'customer_name', 'tranzila_doc_id',
        ]

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
    linked_invoice_id = serializers.CharField(required=False, allow_blank=True, default='')
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

    invoice_details = InvoiceDetailsInputSerializer(required=False)
    receipt_details = ReceiptDetailsInputSerializer(required=False)
    credit_invoice_details = CreditInvoiceInputSerializer(required=False)


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


class CreateCheckPlanSerializer(serializers.Serializer):
    child_id = serializers.UUIDField()
    lesson_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    checks = serializers.ListField(child=serializers.DictField(), allow_empty=False)
