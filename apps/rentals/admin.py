import json

from django.contrib import admin
from django.utils.html import format_html

from apps.rentals.models import RentalContract, Tenancy


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


@admin.register(RentalContract)
class RentalContractAdmin(admin.ModelAdmin):
    """
    Issued contracts, to look at. Nothing here writes: a contract is issued and
    voided through the API (apps/rentals/contracts.py), which keeps the
    versions, the fingerprints and the voiding of the previous one together.
    """

    list_display = ['tenancy', 'version', 'status', 'created_at', 'created_by', 'voided_at']
    list_filter = ['status']
    search_fields = [
        'tenancy__tenant__first_name', 'tenancy__tenant__last_name',
        'tenancy__tenant__company_number', 'tenancy__tenant__id_number', 'terms_sha256',
    ]
    list_select_related = ['tenancy__tenant', 'created_by']
    # The signing token is left out on purpose: it opens the tenant's page to whoever holds it.
    fields = [
        'tenancy', 'version', 'status', 'created_at', 'created_by', 'voided_at', 'void_reason',
        'terms_sha256', 'terms_display', 'pdf_sha256', 'pdf_size',
        'sign_token_created_at', 'sent_at', 'viewed_at', 'signed_at', 'signature', 'signed_pdf_sha256',
    ]
    readonly_fields = fields

    def get_queryset(self, request):
        # The list never shows the files; the detail page loads the issued one for its size.
        return super().get_queryset(request).defer('pdf', 'signed_pdf')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='תנאי החוזה')
    def terms_display(self, obj):
        return format_html(
            '<pre dir="rtl" style="white-space: pre-wrap; margin: 0">{}</pre>',
            json.dumps(obj.terms, ensure_ascii=False, indent=2, sort_keys=True),
        )

    @admin.display(description='גודל הקובץ')
    def pdf_size(self, obj):
        return f'{len(bytes(obj.pdf or b"")) / 1024:.0f} KB'
