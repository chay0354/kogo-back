from rest_framework import serializers

from apps.legacy_import.models import LegacyDocument, LegacyImport
from apps.legacy_import.sources import source_label


class LegacyDocumentSerializer(serializers.ModelSerializer):
    """A document from the previous software. `source` says so on every row, so
    no screen can mistake it for one kogo issued."""

    source = serializers.SerializerMethodField()
    doc_type_label = serializers.CharField(source='get_doc_type_display', read_only=True)
    business_customer_id = serializers.UUIDField(read_only=True, allow_null=True)
    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')
    source_label = serializers.SerializerMethodField()
    # Whether the software's PDF was received (its fingerprint is kept), and whether it is in the locked bucket.
    has_pdf = serializers.SerializerMethodField()
    pdf_stored = serializers.SerializerMethodField()

    class Meta:
        model = LegacyDocument
        fields = [
            'id', 'source', 'source_system', 'source_label', 'doc_type', 'doc_type_label', 'original_type',
            'number', 'original_number', 'document_date',
            'invoice_total', 'receipt_total', 'credit_total', 'withholding_amount', 'total_before_withholding',
            'amount_before_vat', 'vat_amount', 'allocation_number', 'linked_document',
            'original_status', 'payment_type', 'card_last_four', 'location', 'details', 'remark',
            'customer_name', 'customer_email', 'customer_phone', 'business_customer_id',
            'business_name', 'business_category_name', 'branch_name',
            'has_pdf', 'pdf_stored', 'pdf_sha256',
        ]
        read_only_fields = fields

    def get_source(self, obj):
        return 'legacy'

    def get_source_label(self, obj):
        return source_label(obj.source_system)

    def get_has_pdf(self, obj):
        return bool(obj.pdf_sha256)

    def get_pdf_stored(self, obj):
        return bool(obj.pdf_object)


class LegacyImportSerializer(serializers.ModelSerializer):
    """An import without its rows: the rows are for the commit, not for the screen."""

    uploaded_by_name = serializers.SerializerMethodField()
    source_label = serializers.SerializerMethodField()

    class Meta:
        model = LegacyImport
        fields = [
            'id', 'source_system', 'source_label', 'file_name', 'sha256', 'row_count', 'status', 'summary', 'mapping',
            'include_subscription_parents', 'result', 'uploaded_at', 'uploaded_by_name', 'committed_at',
        ]
        read_only_fields = fields

    def get_source_label(self, obj):
        return source_label(obj.source_system)

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        if user is None:
            return ''
        return user.get_full_name() or user.get_username()


class LegacyImportListSerializer(LegacyImportSerializer):
    class Meta(LegacyImportSerializer.Meta):
        fields = [
            'id', 'source_system', 'source_label', 'file_name', 'row_count', 'status', 'result', 'uploaded_at',
            'uploaded_by_name', 'committed_at',
        ]
        read_only_fields = fields
