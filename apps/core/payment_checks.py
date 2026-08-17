"""
Django system checks for the payment configuration.

These run on `manage.py check`, `runserver` and every deploy command, so a
production instance cannot quietly boot with placeholder gateway credentials.
Use `manage.py check_tranzila` for the deeper check that also calls Tranzila.
"""
from django.conf import settings
from django.core.checks import Warning as CheckWarning, register

from apps.core.tranzila_service import is_mock_credential

PLACEHOLDER_ID = 'payments.W001'
MISSING_KEYS_ID = 'payments.W002'
NOTIFY_URL_ID = 'payments.W003'


def _running_tests() -> bool:
    import sys
    return any(arg == 'test' or arg.endswith('pytest') for arg in sys.argv)


@register()
def check_payment_configuration(app_configs, **kwargs):
    if settings.DEBUG or _running_tests():
        return []

    issues = []

    placeholders = [
        name for name, value in (
            ('TRANZILA_TERMINAL', getattr(settings, 'TRANZILA_TERMINAL', '')),
            ('TRANZILA_SUPPLIER', getattr(settings, 'TRANZILA_SUPPLIER', '')),
            ('TRANZILA_PUBLIC_KEY', getattr(settings, 'TRANZILA_PUBLIC_KEY', '')),
            ('TRANZILA_SECRET_KEY', getattr(settings, 'TRANZILA_SECRET_KEY', '')),
        )
        if is_mock_credential(value)
    ]
    if placeholders:
        issues.append(CheckWarning(
            'Tranzila credentials still hold placeholder values: ' + ', '.join(placeholders),
            hint='Set the real terminal and API keys; charges are refused while placeholders remain.',
            id=PLACEHOLDER_ID,
        ))

    missing = [
        name for name, value in (
            ('TRANZILA_PUBLIC_KEY', getattr(settings, 'TRANZILA_PUBLIC_KEY', '')),
            ('TRANZILA_SECRET_KEY', getattr(settings, 'TRANZILA_SECRET_KEY', '')),
            ('TRANZILA_WEBHOOK_SECRET', getattr(settings, 'TRANZILA_WEBHOOK_SECRET', '')),
        )
        if not value
    ]
    if missing:
        issues.append(CheckWarning(
            'Tranzila settings are empty: ' + ', '.join(missing),
            hint='Card charges and webhook verification need these values.',
            id=MISSING_KEYS_ID,
        ))

    if not getattr(settings, 'CRM_API_BASE_URL', ''):
        issues.append(CheckWarning(
            'CRM_API_BASE_URL is not set, so Tranzila iframe payments have no notify URL.',
            hint='Set it to this API\'s public origin, e.g. https://api.example.com',
            id=NOTIFY_URL_ID,
        ))

    return issues
