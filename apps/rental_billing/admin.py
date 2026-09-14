"""Standing orders, charges and card links, to look at. Nothing here writes.

Every change goes through the API (orders.py, billing.py), which keeps the
reservation, the schedule and the receipt together. The card token is never shown.
"""
from django.contrib import admin

from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TenantStandingOrder)
class TenantStandingOrderAdmin(_ReadOnlyAdmin):
    list_display = ['tenant', 'branch', 'status', 'amount_before_vat', 'billing_day', 'next_charge_date', 'card_last4']
    list_filter = ['status', 'source', 'branch']
    search_fields = ['tenant__first_name', 'tenant__last_name', 'tenant__company_number', 'tenant__id_number']
    list_select_related = ['tenant', 'branch']
    exclude = ['tranzila_token']


@admin.register(TenantCharge)
class TenantChargeAdmin(_ReadOnlyAdmin):
    list_display = ['standing_order', 'period', 'status', 'total', 'transaction_id', 'receipt', 'charged_at']
    list_filter = ['status', 'trigger']
    search_fields = ['transaction_id', 'confirmation_code', 'standing_order__tenant__last_name']
    list_select_related = ['standing_order__tenant', 'receipt']


@admin.register(TenantCardLink)
class TenantCardLinkAdmin(_ReadOnlyAdmin):
    list_display = ['standing_order', 'status', 'attempts', 'created_at', 'used_at']
    list_filter = ['status']
    list_select_related = ['standing_order__tenant']
    exclude = ['token']
