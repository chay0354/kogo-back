from django.apps import AppConfig


class CustomersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.customers'

    def ready(self):
        # Import models to ensure they're registered
        from apps.customers import (  # noqa
            financial_models, identification_models, location_models, status_history_models,
        )
        # Import signals to track status changes
        from apps.customers import signals  # noqa
        # store_models moved to apps.store

