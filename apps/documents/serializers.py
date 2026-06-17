from rest_framework import serializers
from apps.documents.models import FormalDocument, DocumentLineItem, DocumentPayment


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

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display',
            'client_type', 'child', 'business_customer',
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

    class Meta:
        model = FormalDocument
        fields = [
            'id', 'document_number', 'document_type', 'document_type_display',
            'document_date', 'total_amount', 'currency', 'tranzila_issued', 'pdf_url',
        ]


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
        'tax_invoice', 'receipt', 'combined', 'transaction_invoice', 'credit_invoice'
    ])
    client_type = serializers.ChoiceField(choices=['business', 'existing'])
    child_id = serializers.UUIDField(required=False, allow_null=True)
    business_customer_id = serializers.UUIDField(required=False, allow_null=True)
    branch_id = serializers.IntegerField(required=False, allow_null=True)
    document_date = serializers.DateField(required=False)

    invoice_details = InvoiceDetailsInputSerializer(required=False)
    receipt_details = ReceiptDetailsInputSerializer(required=False)
    credit_invoice_details = CreditInvoiceInputSerializer(required=False)
