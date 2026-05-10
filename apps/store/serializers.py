"""
Store Serializers - API Serialization for Store Models
"""
from django.db import transaction
from rest_framework import serializers
from apps.store.models import StoreProduct, StoreProductSize, StoreInvoice, StoreSale
from apps.core.models import Branch
from apps.customers.models import Child


class StoreProductSizeSerializer(serializers.ModelSerializer):
    """Serializer for per-size stock rows."""

    class Meta:
        model = StoreProductSize
        fields = ['id', 'size', 'stock_quantity', 'sort_order']
        read_only_fields = ['id']


def _normalize_size_stocks(value):
    """
    Coerce incoming size_stocks into a clean, deduplicated list.

    Rules:
    - Each entry must be {size: str, stock_quantity: int >= 0}.
    - Sizes are stripped; empty sizes are dropped.
    - Same size repeated → last one wins.
    - sort_order is filled in from the input order if not provided.
    """
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError("size_stocks חייב להיות רשימה")

    cleaned: dict[str, dict] = {}
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise serializers.ValidationError(
                "כל פריט מידה חייב להיות אובייקט עם size ו-stock_quantity"
            )
        size_label = str(entry.get('size', '')).strip()
        if not size_label:
            continue
        try:
            qty = int(entry.get('stock_quantity', 0) or 0)
        except (TypeError, ValueError):
            raise serializers.ValidationError("stock_quantity לכל מידה חייב להיות מספר שלם")
        if qty < 0:
            raise serializers.ValidationError("stock_quantity לכל מידה לא יכול להיות שלילי")

        try:
            sort_order = int(entry.get('sort_order', index))
        except (TypeError, ValueError):
            sort_order = index

        cleaned[size_label] = {
            'size': size_label,
            'stock_quantity': qty,
            'sort_order': sort_order,
        }

    return [cleaned[size] for size in sorted(cleaned, key=lambda s: cleaned[s]['sort_order'])]


class StoreProductSerializer(serializers.ModelSerializer):
    """Serializer for StoreProduct model."""

    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    profit_margin = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    size_stocks = StoreProductSizeSerializer(many=True, required=False)

    class Meta:
        model = StoreProduct
        fields = [
            'id', 'name', 'category', 'size',
            'cost_price', 'sale_price',
            'branch', 'branch_name',
            'stock_quantity', 'min_stock_alert', 'is_low_stock',
            'image_url', 'notes', 'is_active',
            'profit_margin',
            'size_stocks',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_size_stocks(self, value):
        return _normalize_size_stocks(value)

    def validate(self, data):
        """Validate product data."""
        # Ensure sale price is greater than cost price
        sale_price = data.get('sale_price', getattr(self.instance, 'sale_price', None))
        cost_price = data.get('cost_price', getattr(self.instance, 'cost_price', None))

        if sale_price and cost_price and sale_price <= cost_price:
            raise serializers.ValidationError(
                "מחיר מכירה חייב להיות גבוה ממחיר עלות (Sale price must be higher than cost price)"
            )

        return data

    def _sync_size_stocks(self, product, size_stocks):
        """
        Replace the product's size rows with the provided list and keep the
        derived `size` (CSV) and `stock_quantity` (total) fields in sync.
        """
        product.size_stocks.all().delete()
        for entry in size_stocks:
            StoreProductSize.objects.create(product=product, **entry)

        if size_stocks:
            product.size = ','.join(entry['size'] for entry in size_stocks)
            product.stock_quantity = sum(entry['stock_quantity'] for entry in size_stocks)
            product.save(update_fields=['size', 'stock_quantity', 'updated_at'])

    def create(self, validated_data):
        size_stocks = validated_data.pop('size_stocks', None)
        with transaction.atomic():
            product = super().create(validated_data)
            if size_stocks is not None:
                self._sync_size_stocks(product, size_stocks)
        return product

    def update(self, instance, validated_data):
        size_stocks = validated_data.pop('size_stocks', None)
        with transaction.atomic():
            product = super().update(instance, validated_data)
            if size_stocks is not None:
                self._sync_size_stocks(product, size_stocks)
        return product


class StoreSaleSerializer(serializers.ModelSerializer):
    """Serializer for StoreSale model (line items)."""
    
    product_name = serializers.CharField(source='product.name', read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    invoice_status = serializers.CharField(source='invoice.payment_status', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    class Meta:
        model = StoreSale
        fields = [
            'id', 'invoice', 'invoice_number', 'invoice_status',
            'product', 'product_name',
            'child', 'child_name',
            'quantity', 'unit_price', 'total_price', 'size',
            'payment_method', 'branch', 'branch_name',
            'sale_date', 'created_at'
        ]
        read_only_fields = ['id', 'sale_date', 'created_at']


class StoreInvoiceSerializer(serializers.ModelSerializer):
    """Serializer for StoreInvoice model."""
    
    line_items = StoreSaleSerializer(many=True, read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    
    class Meta:
        model = StoreInvoice
        fields = [
            'id', 'invoice_number',
            'child', 'child_name', 'customer_name', 'customer_phone',
            'total_amount', 'refunded_amount', 'payment_method', 'payment_status',
            'tranzila_transaction_id', 'tranzila_confirmation_code',
            'charged_with_token',
            'branch', 'branch_name',
            'issue_date', 'notes',
            'line_items',
            'created_at'
        ]
        read_only_fields = ['id', 'invoice_number', 'issue_date', 'created_at', 'refunded_amount']


class StoreAnalyticsSerializer(serializers.Serializer):
    """Serializer for store analytics dashboard data."""
    
    # KPIs
    total_revenue = serializers.DecimalField(max_digits=10, decimal_places=2)
    net_profit = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_sales_count = serializers.IntegerField()
    low_stock_count = serializers.IntegerField()
    
    # Charts data
    monthly_revenue = serializers.ListField(child=serializers.DictField())
    sales_by_product = serializers.ListField(child=serializers.DictField())
    sales_by_category = serializers.ListField(child=serializers.DictField())
    sales_by_branch = serializers.ListField(child=serializers.DictField())
    sales_by_payment_method = serializers.ListField(child=serializers.DictField())
    
    # Lists
    low_stock_products = StoreProductSerializer(many=True)
    recent_sales = StoreSaleSerializer(many=True)


class PaymentInitiationResponseSerializer(serializers.Serializer):
    """Response serializer for payment initiation."""
    
    requires_iframe = serializers.BooleanField()
    iframe_url = serializers.URLField(required=False, allow_null=True)
    invoice_id = serializers.UUIDField(required=False, allow_null=True)
    invoice = StoreInvoiceSerializer(required=False, allow_null=True)
    success = serializers.BooleanField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_null=True)

