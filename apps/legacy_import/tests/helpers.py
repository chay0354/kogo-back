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


# --------------------------------------------------------------------------
# Other softwares' files, built in the test: a CSV, a minimal .xlsx
# --------------------------------------------------------------------------

def csv_bytes(table, delimiter=',', encoding='utf-8', bom=False):
    """A CSV of `table` (a list of rows), the way another software would write it."""
    import csv
    import io

    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator='\r\n').writerows(table)
    data = buffer.getvalue().encode(encoding)
    return (b'\xef\xbb\xbf' + data) if bom else data


def _xlsx_cell(ref, value, strings):
    from datetime import date as _date, datetime as _datetime
    from xml.sax.saxutils import escape

    if value is None:
        return ''
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (_datetime, _date)):
        start = _datetime(1899, 12, 30)
        moment = value if isinstance(value, _datetime) else _datetime(value.year, value.month, value.day)
        serial = (moment - start).days + (moment - start).seconds / 86400
        return f'<c r="{ref}" s="1"><v>{serial}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if str(value).startswith('inline:'):
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value)[7:])}</t></is></c>'
    strings.append(str(value))
    return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'


def xlsx_bytes(table):
    """
    A minimal .xlsx of `table`: shared strings, one date style (a custom
    dd/mm/yyyy format), numbers and inline strings ('inline:…') — the parts
    tables._read_xlsx reads, as Excel lays them out.
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    strings, rows = [], []
    for r, record in enumerate(table, start=1):
        cells = ''.join(
            _xlsx_cell(f'{chr(65 + c)}{r}', value, strings) for c, value in enumerate(record)
        )
        rows.append(f'<row r="{r}">{cells}</row>')
    main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    parts = {
        'xl/workbook.xml': (
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            '<sheet name="Documents" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        'xl/_rels/workbook.xml.rels': (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ),
        'xl/styles.xml': (
            f'<styleSheet xmlns="{main}"><numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/>'
            '</numFmts><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/>'
            '</cellXfs></styleSheet>'
        ),
        'xl/sharedStrings.xml': (
            f'<sst xmlns="{main}" count="{len(strings)}">'
            + ''.join(f'<si><t>{escape(s)}</t></si>' for s in strings) + '</sst>'
        ),
        'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{main}"><sheetData>{"".join(rows)}</sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + xml)
    return buffer.getvalue()
