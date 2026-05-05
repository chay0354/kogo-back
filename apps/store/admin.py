"""
Store Admin - Django Admin Configuration
"""
from django.contrib import admin
from apps.store.models import StoreProduct, StoreInvoice, StoreSale


@admin.register(StoreProduct)
class StoreProductAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'category', 'sale_price', 'cost_price',
        'stock_quantity', 'min_stock_alert', 'is_low_stock',
        'branch', 'is_active'
    ]
    list_filter = ['category', 'branch', 'is_active', 'created_at']
    search_fields = ['name', 'category', 'notes']
    readonly_fields = ['id', 'created_at', 'updated_at']
    fieldsets = (
        ('פרטי מוצר', {
            'fields': ('name', 'category', 'size')
        }),
        ('תמחור', {
            'fields': ('cost_price', 'sale_price')
        }),
        ('מלאי', {
            'fields': ('stock_quantity', 'min_stock_alert', 'branch')
        }),
        ('נוספים', {
            'fields': ('image_url', 'notes', 'is_active')
        }),
        ('מידע מערכת', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def is_low_stock(self, obj):
        return obj.is_low_stock
    is_low_stock.boolean = True
    is_low_stock.short_description = 'מלאי נמוך'


class StoreSaleInline(admin.TabularInline):
    model = StoreSale
    extra = 0
    readonly_fields = ['id', 'sale_date', 'created_at']
    fields = [
        'product', 'quantity', 'size', 'unit_price', 'total_price',
        'payment_method'
    ]


@admin.register(StoreInvoice)
class StoreInvoiceAdmin(admin.ModelAdmin):
    list_display = [
        'invoice_number', 'get_customer_name', 'total_amount',
        'payment_method', 'payment_status', 'charged_with_token',
        'issue_date'
    ]
    list_filter = ['payment_method', 'payment_status', 'charged_with_token', 'issue_date']
    search_fields = ['invoice_number', 'customer_name', 'child__first_name', 'child__last_name']
    readonly_fields = ['id', 'invoice_number', 'issue_date', 'created_at']
    inlines = [StoreSaleInline]
    fieldsets = (
        ('מזהים', {
            'fields': ('invoice_number', 'id')
        }),
        ('לקוח', {
            'fields': ('child', 'customer_name', 'customer_phone')
        }),
        ('תשלום', {
            'fields': ('total_amount', 'payment_method', 'payment_status')
        }),
        ('טרנזילה', {
            'fields': (
                'tranzila_transaction_id', 'tranzila_confirmation_code',
                'charged_with_token'
            ),
            'classes': ('collapse',)
        }),
        ('נוספים', {
            'fields': ('branch', 'notes')
        }),
        ('תאריכים', {
            'fields': ('issue_date', 'created_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_customer_name(self, obj):
        if obj.child:
            return obj.child.full_name
        return obj.customer_name or 'Walk-in'
    get_customer_name.short_description = 'לקוח'


@admin.register(StoreSale)
class StoreSaleAdmin(admin.ModelAdmin):
    list_display = [
        'invoice', 'product', 'quantity', 'total_price',
        'payment_method', 'sale_date'
    ]
    list_filter = ['payment_method', 'sale_date', 'branch']
    search_fields = ['invoice__invoice_number', 'product__name', 'child__first_name', 'child__last_name']
    readonly_fields = ['id', 'sale_date', 'created_at']
    fieldsets = (
        ('מכירה', {
            'fields': ('invoice', 'product', 'quantity', 'size')
        }),
        ('מחיר', {
            'fields': ('unit_price', 'total_price')
        }),
        ('לקוח ותשלום', {
            'fields': ('child', 'payment_method', 'branch')
        }),
        ('תאריכים', {
            'fields': ('sale_date', 'created_at'),
            'classes': ('collapse',)
        }),
    )

