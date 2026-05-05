from django.contrib import admin
from .models import (
    Family, Parent, Child, Payment, RecurringPayment,
    TranzilaTransaction, PaymentDiscountSnapshot
)
from .status_history_models import ChildStatusHistory
from .financial_models import Invoice, InvoiceChild, InvoiceActivityLog, Discount
# Store models moved to apps.store


class ParentInline(admin.TabularInline):
    model = Parent
    extra = 1


class ChildInline(admin.TabularInline):
    model = Child
    extra = 1
    fields = ['first_name', 'last_name', 'birth_date', 'gender', 'status', 'id_number']
    readonly_fields = ['status']


class EnrollmentInline(admin.TabularInline):
    """Show course enrollments for a child"""
    from apps.enrollments.models import Enrollment
    model = Enrollment
    extra = 0
    fields = ['course', 'is_active', 'enrolled_at']
    readonly_fields = ['enrolled_at']
    can_delete = True


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ['name', 'phone', 'email', 'parent_id_number', 'branch', 'created_at']
    list_filter = ['branch', 'created_at']
    search_fields = ['name', 'phone', 'email', 'parent_id_number', 'address']
    inlines = [ParentInline, ChildInline]
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('פרטי משפחה', {
            'fields': ('name', 'parent_id_number', 'phone', 'email', 'address', 'branch')
        }),
        ('מידע נוסף', {
            'fields': ('notes', 'created_at', 'updated_at'),
        }),
    )


@admin.register(Parent)
class ParentAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'family', 'phone', 'email', 'is_primary']
    list_filter = ['is_primary']
    search_fields = ['first_name', 'last_name', 'phone', 'email']


class ChildStatusHistoryInline(admin.TabularInline):
    """Show status history for a child"""
    model = ChildStatusHistory
    extra = 0
    fields = ['previous_status', 'new_status', 'changed_at', 'reason', 'changed_by']
    readonly_fields = ['previous_status', 'new_status', 'changed_at', 'reason', 'changed_by']
    can_delete = False
    ordering = ['-changed_at']
    
    def has_add_permission(self, request, obj=None):
        """Prevent manual creation - status changes are tracked automatically"""
        return False


@admin.register(Child)
class ChildAdmin(admin.ModelAdmin):
    list_display = [
        'full_name', 'family', 'birth_date', 'age', 'gender', 
        'status', 'absent_irregularly', 'paid_until_date', 'subscription_start_date', 'subscription_end_date'
    ]
    list_filter = ['gender', 'status', 'absent_irregularly', 'subscription_start_date']
    search_fields = ['first_name', 'last_name', 'id_number', 'family__name']
    readonly_fields = ['age', 'full_name', 'created_at', 'updated_at']
    inlines = [EnrollmentInline, ChildStatusHistoryInline]
    
    fieldsets = (
        ('פרטים אישיים', {
            'fields': ('first_name', 'last_name', 'full_name', 'id_number', 'phone_number', 'birth_date', 'age', 'gender', 'family')
        }),
        ('סטטוס ומנוי', {
            'fields': ('status', 'subscription_start_date', 'subscription_end_date', 'paid_until_date', 'trial_classes_attended'),
            'description': 'הסטטוס מתעדכן אוטומטית על סמך התאריכים. שינויי סטטוס נרשמים אוטומטית להיסטוריה.'
        }),
        ('נוכחות', {
            'fields': ('absent_irregularly',),
            'description': 'מסומן כ-True אם הילד נעדר 3 פעמים עם פחות מ-8 ימים בין ההיעדרויות'
        }),
        ('הערות', {
            'fields': ('notes',),
        }),
        ('מטא-דאטה', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    actions = ['update_status_action']
    
    def update_status_action(self, request, queryset):
        """עדכן סטטוס עבור ילדים נבחרים"""
        count = 0
        for child in queryset:
            child.update_status()
            count += 1
        self.message_user(request, f'סטטוס עודכן עבור {count} ילדים')
    update_status_action.short_description = 'עדכן סטטוס אוטומטית'
    
    def absent_irregularly(self, obj):
        """Display irregular attendance status with icon"""
        if obj.absent_irregularly:
            return '⚠️ כן'
        return 'לא'
    absent_irregularly.short_description = 'נוכחות חריגה'
    absent_irregularly.admin_order_field = 'absent_irregularly'


# Financial Models
class InvoiceChildInline(admin.TabularInline):
    model = InvoiceChild
    extra = 1


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'family', 'amount', 'status', 'payment_method', 'invoice_date']
    list_filter = ['status', 'payment_method', 'branch']
    search_fields = ['invoice_number', 'family__name', 'payer_name']
    inlines = [InvoiceChildInline]


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ['name', 'discount_type', 'value', 'applies_to', 'promotion_type', 'is_active']
    list_filter = ['discount_type', 'applies_to', 'promotion_type', 'is_active']
    search_fields = ['name', 'description']


# Store admin moved to apps.store.admin


# ============================================================================
# Payment Admin - Tranzila Integration
# ============================================================================

class PaymentDiscountSnapshotInline(admin.TabularInline):
    """צילומי הנחות בתשלום"""
    model = PaymentDiscountSnapshot
    extra = 0
    fields = ['discount_name', 'discount_type', 'discount_value', 'amount_deducted', 'reason']
    readonly_fields = ['discount_name', 'discount_type', 'discount_value', 'amount_deducted', 'reason']
    can_delete = False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """תשלומים"""
    list_display = [
        'id', 'child', 'family', 'payment_type', 'status',
        'base_amount', 'discount_amount', 'final_amount',
        'payment_date', 'created_at'
    ]
    list_filter = ['status', 'payment_type', 'created_at', 'payment_date']
    search_fields = [
        'child__first_name', 'child__last_name',
        'family__name', 'id'
    ]
    readonly_fields = [
        'id', 'base_amount', 'discount_amount', 'final_amount',
        'tranzila_transaction', 'created_at', 'updated_at'
    ]
    inlines = [PaymentDiscountSnapshotInline]
    
    fieldsets = (
        ('פרטי תשלום', {
            'fields': ('id', 'child', 'family', 'parent', 'branch')
        }),
        ('סכומים', {
            'fields': ('payment_type', 'status', 'base_amount', 'discount_amount', 'final_amount')
        }),
        ('תאריכים', {
            'fields': ('payment_date', 'created_at', 'updated_at')
        }),
        ('פרטים נוספים', {
            'fields': ('description', 'tranzila_transaction')
        }),
        ('כישלון', {
            'fields': ('failure_reason', 'failure_code'),
            'classes': ('collapse',)
        }),
    )


@admin.register(RecurringPayment)
class RecurringPaymentAdmin(admin.ModelAdmin):
    """מנויים חוזרים"""
    list_display = [
        'id', 'child', 'status', 'amount', 'billing_day',
        'next_billing_date', 'start_date', 'created_at'
    ]
    list_filter = ['status', 'created_at', 'start_date']
    search_fields = ['child__first_name', 'child__last_name', 'id']
    readonly_fields = [
        'id', 'tranzila_token', 'tranzila_recurring_index',
        'created_at', 'updated_at'
    ]
    
    fieldsets = (
        ('פרטי מנוי', {
            'fields': ('id', 'child', 'initial_payment', 'status')
        }),
        ('חיוב', {
            'fields': ('amount', 'billing_day', 'start_date', 'end_date', 'next_billing_date', 'last_charge_date')
        }),
        ('טרנזילה', {
            'fields': ('tranzila_token', 'tranzila_recurring_index'),
            'classes': ('collapse',)
        }),
        ('ביטול', {
            'fields': ('cancelled_at', 'cancellation_reason'),
            'classes': ('collapse',)
        }),
        ('מטא-דאטה', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(TranzilaTransaction)
class TranzilaTransactionAdmin(admin.ModelAdmin):
    """עסקאות טרנזילה"""
    list_display = [
        'transaction_id', 'transaction_type', 'response_code',
        'is_successful', 'created_at'
    ]
    list_filter = ['transaction_type', 'is_successful', 'created_at']
    search_fields = ['transaction_id', 'confirmation_code', 'idempotency_key']
    readonly_fields = [
        'id', 'transaction_id', 'confirmation_code', 'transaction_type',
        'response_code', 'response_message', 'request_data', 'response_data',
        'idempotency_key', 'is_successful', 'request_timestamp',
        'response_timestamp', 'created_at'
    ]
    
    fieldsets = (
        ('פרטי עסקה', {
            'fields': ('id', 'transaction_id', 'confirmation_code', 'transaction_type')
        }),
        ('תגובה', {
            'fields': ('response_code', 'response_message', 'is_successful')
        }),
        ('נתונים גולמיים', {
            'fields': ('request_data', 'response_data'),
            'classes': ('collapse',)
        }),
        ('מטא-דאטה', {
            'fields': ('idempotency_key', 'request_timestamp', 'response_timestamp', 'created_at'),
            'classes': ('collapse',)
        }),
    )
    
    def has_add_permission(self, request):
        """Prevent manual creation of transactions"""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Prevent deletion of transactions for audit trail"""
        return False


@admin.register(PaymentDiscountSnapshot)
class PaymentDiscountSnapshotAdmin(admin.ModelAdmin):
    """צילומי הנחות"""
    list_display = ['payment', 'discount_name', 'discount_type', 'amount_deducted', 'created_at']
    list_filter = ['discount_type', 'created_at']
    search_fields = ['payment__id', 'discount_name']
    readonly_fields = ['payment', 'discount', 'discount_name', 'discount_type', 'discount_value', 'amount_deducted', 'reason', 'created_at']
    
    def has_add_permission(self, request):
        """Prevent manual creation"""
        return False


# ============================================================================
# Status History Admin
# ============================================================================

@admin.register(ChildStatusHistory)
class ChildStatusHistoryAdmin(admin.ModelAdmin):
    """היסטוריית שינויי סטטוס של ילדים"""
    list_display = ['child', 'previous_status', 'new_status', 'changed_at', 'changed_by']
    list_filter = ['previous_status', 'new_status', 'changed_at']
    search_fields = ['child__first_name', 'child__last_name', 'child__family__name', 'reason']
    readonly_fields = ['child', 'previous_status', 'new_status', 'changed_at', 'reason', 'changed_by', 'created_at']
    date_hierarchy = 'changed_at'
    
    fieldsets = (
        ('שינוי סטטוס', {
            'fields': ('child', 'previous_status', 'new_status', 'changed_at')
        }),
        ('פרטים נוספים', {
            'fields': ('reason', 'changed_by')
        }),
        ('מטא-דאטה', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )
    
    def has_add_permission(self, request):
        """Prevent manual creation - status changes are tracked automatically via signals"""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Prevent deletion for audit trail integrity"""
        return False

