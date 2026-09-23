from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.documents'
    verbose_name = 'מסמכים פיננסיים'

    def ready(self):
        from django.core.signals import request_finished, request_started

        from apps.documents.signing.service import clear_inline_budget, reset_inline_budget

        # Each request signs a few archive-only originals on the spot and leaves
        # the rest to the sign-pending cron (SIGNING_INLINE_BUDGET).
        request_started.connect(reset_inline_budget, dispatch_uid='documents_signing_budget_start')
        request_finished.connect(clear_inline_budget, dispatch_uid='documents_signing_budget_end')
