from django.contrib import admin

from apps.signatures.models import Signature


@admin.register(Signature)
class SignatureAdmin(admin.ModelAdmin):
    """Read-only: a stored signature is evidence, and the model refuses edits anyway."""

    list_display = ['signed_at', 'kind', 'signer_name', 'signer_id_number', 'family', 'branch', 'source']
    list_filter = ['kind', 'source', 'branch']
    search_fields = ['signer_name', 'signer_id_number', 'signer_phone', 'signer_email']
    date_hierarchy = 'signed_at'
    list_select_related = ['family', 'branch']
    exclude = ['document_html']

    def get_queryset(self, request):
        return super().get_queryset(request).defer('signature_png', 'document_html')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
