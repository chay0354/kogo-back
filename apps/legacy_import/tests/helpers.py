"""Synthetic rows for the import tests. Every name, number and address is invented."""
from pathlib import Path

from django.contrib.auth import get_user_model

from apps.core.models import UserProfile
from apps.legacy_import.parser import COLUMNS, customer_key
from apps.legacy_import.reader import Sheet

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SAMPLE_XLS = FIXTURES / 'export_sample.xls'
CORRUPT_XLS = FIXTURES / 'export_sample_corrupt.xls'
# What the fixtures carry in the app-password column; it must never come back out.
FIXTURE_PASSWORD = 'SECRET-PW-123'

_counter = {'n': 1000}


def row(**overrides):
    """One normalised row, as parser.parse_sheet returns it."""
    _counter['n'] += 1
    base = {
        'type_label': 'חשבונית מס קבלה',
        'doc_type': 'combined',
        'number': _counter['n'],
        'date': '2025-01-01',
        'invoice_total': '236.00',
        'receipt_total': '236.00',
        'credit_total': '0.00',
        'withholding': '0.00',
        'before_withholding': '236.00',
        'status': 'סגורה',
        'payment_type': 'כרטיס אשראי',
        'card_last_four': '1234',
        'location': 'כפר סבא',
        'details': 'חוג',
        'remark': '',
        'first_name': 'דנה',
        'last_name': 'בדיקה',
        'email': '',
        'phone': '',
        'id_number': '',
        'ext_number': '1',
        'city': '',
        'address': '',
        'customer_notes': '',
        'deleted': False,
        'dealer_number': '',
    }
    base.update(overrides)
    if 'customer_key' not in overrides:
        base['customer_key'] = customer_key(base['id_number'], base['ext_number'], base['email'], base['phone'])
    return base


HEADER = {name: labels[0] for name, labels in COLUMNS.items()}


def sheet(records, extra_headers=(), order=None):
    """
    A reader.Sheet with the old software's headers. `records` are dicts of
    header -> raw cell value (what xlrd would hand back).
    """
    headers = list(order) if order else list(HEADER.values()) + list(extra_headers)
    return Sheet(headers=headers, rows=[[record.get(h) for h in headers] for record in records])


def make_user(username, role):
    user = get_user_model().objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return get_user_model().objects.get(pk=user.pk)
