from rest_framework import serializers

from apps.legacy_import.models import LegacyDocument, LegacyImport


class LegacyDocumentSerializer(serializers.ModelSerializer):
    """A document from the previous software. `source` says so on every row, so
    no screen can mistake it for one kogo issued."""

    source = serializers.SerializerMethodField()
    doc_type_label = serializers.CharField(source='get_doc_type_display', read_only=True)
    business_customer_id = serializers.UUIDField(read_only=True, allow_null=True)
    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')

    class Meta:
        model = LegacyDocument
        fields = [
            'id', 'source', 'doc_type', 'doc_type_label', 'original_type', 'number', 'document_date',
            'invoice_total', 'receipt_total', 'credit_total', 'withholding_amount', 'total_before_withholding',
            'original_status', 'payment_type', 'card_last_four', 'location', 'details', 'remark',
            'customer_name', 'customer_email', 'customer_phone', 'business_customer_id',
            'business_name', 'business_category_name', 'branch_name',
        ]
        read_only_fields = fields

    def get_source(self, obj):
        return 'legacy'


class LegacyImportSerializer(serializers.ModelSerializer):
    """An import without its rows: the rows are for the commit, not for the screen."""

    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LegacyImport
        fields = [
            'id', 'file_name', 'sha256', 'row_count', 'status', 'summary', 'mapping',
            'include_subscription_parents', 'result', 'uploaded_at', 'uploaded_by_name', 'committed_at',
        ]
        read_only_fields = fields

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        if user is None:
            return ''
        return user.get_full_name() or user.get_username()


class LegacyImportListSerializer(LegacyImportSerializer):
    class Meta(LegacyImportSerializer.Meta):
        fields = [
            'id', 'file_name', 'row_count', 'status', 'result', 'uploaded_at', 'uploaded_by_name', 'committed_at',
        ]
        read_only_fields = fields
