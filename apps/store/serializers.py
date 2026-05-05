"""
Store Serializers - API Serialization for Store Models
"""
from rest_framework import serializers
from apps.store.models import StoreProduct, StoreInvoice, StoreSale
from apps.core.models import Branch
from apps.customers.models import Child


class StoreProductSerializer(serializers.ModelSerializer):
    """Serializer for StoreProduct model."""
    
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    profit_margin = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    
    class Meta:
        model = StoreProduct
        fields = [
            'id', 'name', 'category', 'size',
            'cost_price', 'sale_price',
            'branch', 'branch_name',
            'stock_quantity', 'min_stock_alert', 'is_low_stock',
            'image_url', 'notes', 'is_active',
            'profit_margin',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
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

