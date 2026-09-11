from django.contrib import admin

from apps.rentals.models import Tenancy


@admin.register(Tenancy)
class TenancyAdmin(admin.ModelAdmin):
    list_display = ['tenant', 'branch', 'status', 'monthly_amount']
    list_filter = ['status', 'branch']
    search_fields = [
        'tenant__first_name', 'tenant__last_name', 'tenant__company_number', 'tenant__id_number',
    ]
    list_select_related = ['tenant', 'branch']
    # Every business customer in one dropdown would be the whole merchant list.
    raw_id_fields = ['tenant']
