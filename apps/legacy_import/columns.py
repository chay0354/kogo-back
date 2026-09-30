"""Any software's table: which column is what, and its rows in the import's shape.

The office uploads a table exported from another invoicing software. The
preview suggests, from the headers, which column holds each of FIELDS; the
office confirms or changes that (the mapping), and the rows are read with it —
into exactly the dicts parser.parse_sheet makes of the previous software's
export, so the preview, the commit and everything after them are the same code
for every software.

Only mapped columns are read into the rows. A column the office did not map is
never stored, and a column whose header says it is a password, a birth date or
a card's full number is not even shown as a sample, and cannot be mapped.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from apps.legacy_import.parser import (
    TYPE_LABELS,
    clean_text,
    customer_key,
    normalise_email,
    normalise_id,
    normalise_phone,
    parse_date,
    type_key,
)
from apps.legacy_import.reader import ImportFileError, Sheet

_QUOTES = str.maketrans({'״': '"', '”': '"', '“': '"', '׳': "'", '’': "'", '‘': "'", '`': "'"})
_NOT_WORD = re.compile(r'[\W_]+', re.UNICODE)
_DIGITS = re.compile(r'\d+')


def header_key(value) -> str:
    """'סה״כ כולל מע"מ (₪)' -> 'סהככוללמעמ': letters and digits only, case folded."""
    return _NOT_WORD.sub('', str(value or '').translate(_QUOTES).casefold())


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    synonyms: tuple
    required: bool = False
    hint: str = ''


FIELDS = (
    Field('doc_type', 'סוג מסמך', (
        'סוג מסמך', 'סוג המסמך', 'סוג', 'סוג חשבונית', 'document type', 'doc type', 'type', 'document',
    ), hint='או בחרו סוג אחד לכל הקובץ'),
    Field('number', 'מספר מסמך', (
        'מספר מסמך', 'מספר המסמך', 'מס מסמך', "מס' מסמך", 'מספר חשבונית', 'מס חשבונית', 'מספר קבלה',
        'מספר', "מס'", 'number', 'document number', 'doc number', 'invoice number', 'receipt number', 'no',
        'doc no', 'invoice no',
    ), required=True),
    Field('date', 'תאריך', (
        'תאריך', 'תאריך מסמך', 'תאריך המסמך', 'תאריך הפקה', 'תאריך חשבונית', 'date', 'document date',
        'invoice date', 'issue date', 'doc date',
    ), required=True),
    Field('customer_name', 'שם הלקוח', (
        'שם לקוח', 'שם הלקוח', 'לקוח', 'שם', 'שם העסק', 'customer', 'customer name', 'client', 'client name',
        'name',
    )),
    Field('customer_id', 'ת"ז / ח"פ של הלקוח', (
        'ת"ז', 'ח"פ', 'ע"מ', 'עוסק מורשה', 'מספר עוסק', 'ח.פ', 'ת.ז', 'מספר זהות', 'ת"ז / ח"פ',
        'ת"ז \\ ע"מ \\ ח"פ', 'ח"פ / ת"ז', 'מספר עוסק מורשה', 'vat id', 'vat number', 'tax id',
        'company number', 'business number', 'customer vat', 'id number',
    )),
    Field('customer_number', 'מספר לקוח בתוכנה', (
        'מספר לקוח', 'מס לקוח', "מס' לקוח", 'קוד לקוח', 'מזהה לקוח', 'customer number', 'customer code',
        'client id', 'customer id', 'client number',
    )),
    Field('email', 'אימייל', ('אימייל', 'דוא"ל', 'דואר אלקטרוני', 'מייל', 'email', 'e-mail', 'mail')),
    Field('phone', 'טלפון', (
        'טלפון', 'טלפון נייד', 'נייד', 'פלאפון', 'סלולרי', 'phone', 'mobile', 'cell', 'tel', 'telephone',
    )),
    Field('amount_before_vat', 'סכום לפני מע"מ', (
        'סכום לפני מע"מ', 'לפני מע"מ', 'סה"כ לפני מע"מ', 'סכום ללא מע"מ', 'ללא מע"מ', 'סכום נטו', 'נטו',
        'amount before vat', 'subtotal', 'net', 'net amount', 'amount excl vat', 'before vat', 'total before vat',
    )),
    Field('vat', 'מע"מ', ('מע"מ', 'סכום מע"מ', 'סה"כ מע"מ', 'vat', 'vat amount', 'tax', 'tax amount')),
    Field('total', 'סה"כ כולל מע"מ', (
        'סה"כ', 'סה"כ כולל מע"מ', 'סכום כולל', 'סכום כולל מע"מ', 'סכום', 'סה"כ לתשלום', 'סה"כ מסמך',
        'סה"כ ח-ן', 'total', 'total amount', 'amount', 'grand total', 'total incl vat', 'sum',
    ), hint='או סכום לפני מע"מ ומע"מ'),
    Field('payment_method', 'אמצעי תשלום', (
        'אמצעי תשלום', 'סוג תשלום', 'אופן תשלום', 'דרך תשלום', 'payment method', 'payment type', 'payment',
    )),
    Field('allocation_number', 'מספר הקצאה', (
        'מספר הקצאה', 'מס הקצאה', "מס' הקצאה", 'הקצאה', 'allocation number', 'allocation',
    )),
    Field('linked_document', 'מסמך מקושר', (
        'מסמך מקושר', 'מסמך קשור', 'מסמך בסיס', 'מסמך מקור', 'חשבונית מקור', 'מסמכים מקושרים',
        'linked document', 'related document', 'reference document', 'original invoice',
    )),
    Field('details', 'פרטים / תיאור', (
        'פרטים', 'תיאור', 'פירוט', 'הערות', 'הערה', 'description', 'details', 'notes', 'remarks', 'comment',
    )),
    Field('location', 'סניף / מיקום', ('סניף', 'מיקום', 'מחלקה', 'branch', 'location', 'department')),
)
FIELD_KEYS = tuple(f.key for f in FIELDS)
_BY_KEY = {f.key: f for f in FIELDS}

# A column whose header is one of these is never sampled and never mapped.
SENSITIVE_HEADER = re.compile(
    r'סיסמ|password|passwd|תאריך לידה|birth|cvv|cvc|תוקף|expir|מספר כרטיס מלא|card number', re.IGNORECASE,
)

# A contained synonym must be at least this long: "id" is in "paid".
MIN_CONTAINED = 4
SAMPLES = 3
DISTINCT_LIMIT = 30


def is_sensitive(header) -> bool:
    return bool(SENSITIVE_HEADER.search(str(header or '').translate(_QUOTES)))


def suggest_mapping(headers) -> dict:
    """field -> column index (or None). Exact header names first, then the longest synonym a header contains."""
    keys = [header_key(h) for h in headers]
    candidates = []
    for field in FIELDS:
        for synonym in field.synonyms:
            syn = header_key(synonym)
            if not syn:
                continue
            for index, key in enumerate(keys):
                if not key or is_sensitive(headers[index]):
                    continue
                if key == syn:
                    candidates.append((2, len(syn), -index, field.key, index))
                elif len(syn) >= MIN_CONTAINED and syn in key:
                    candidates.append((1, len(syn), -index, field.key, index))
    candidates.sort(reverse=True)
    mapping, taken = {key: None for key in FIELD_KEYS}, set()
    for _rank, _length, _order, field, index in candidates:
        if mapping[field] is None and index not in taken:
            mapping[field] = index
            taken.add(index)
    return mapping


def clean_mapping(raw, headers) -> dict:
    """The office's mapping, checked: field -> a column index that exists and is not sensitive, or None."""
    if raw in (None, ''):
        return suggest_mapping(headers)
    if not isinstance(raw, dict):
        raise ImportFileError('מיפוי העמודות אינו תקין')
    mapping, used = {}, {}
    for key in FIELD_KEYS:
        value = raw.get(key)
        if value in (None, '', -1):
            mapping[key] = None
            continue
        try:
            index = int(value)
        except (TypeError, ValueError):
            raise ImportFileError(f'מיפוי העמודה של "{_BY_KEY[key].label}" אינו תקין')
        if not 0 <= index < len(headers):
            raise ImportFileError(f'העמודה שנבחרה ל"{_BY_KEY[key].label}" אינה בקובץ')
        if is_sensitive(headers[index]):
            raise ImportFileError(f'העמודה "{headers[index]}" אינה נקראת (מידע רגיש)')
        if index in used:
            raise ImportFileError(
                f'העמודה "{headers[index]}" נבחרה גם ל"{_BY_KEY[used[index]].label}" וגם ל"{_BY_KEY[key].label}"'
            )
        used[index] = key
        mapping[key] = index
    return mapping


def missing_fields(mapping: dict, fixed_doc_type: str = '') -> list:
    """The Hebrew names of what a mapping still lacks for a document to be read."""
    missing = [_BY_KEY[key].label for key in ('number', 'date') if mapping.get(key) is None]
    if mapping.get('doc_type') is None and not fixed_doc_type:
        missing.append('סוג מסמך (עמודה, או סוג אחד לכל הקובץ)')
    if mapping.get('total') is None and mapping.get('amount_before_vat') is None:
        missing.append('סכום (סה"כ, או סכום לפני מע"מ)')
    return missing


def describe(sheet: Sheet) -> dict:
    """What the mapping step shows: every column, a few of its values, and the suggestion."""
    columns = []
    for index, header in enumerate(sheet.headers):
        sensitive = is_sensitive(header)
        values = [] if sensitive else [row[index] for row in sheet.rows if index < len(row)]
        texts = [clean_text(v) for v in values if clean_text(v)]
        distinct = Counter(texts)
        columns.append({
            'index': index,
            'header': header,
            'sensitive': sensitive,
            'samples': [text[:60] for text in texts[:SAMPLES]],
            # A column with few different values is a type or a payment method: all of them, for mapping.
            'distinct': (
                [{'value': value[:80], 'count': count} for value, count in distinct.most_common()]
                if 0 < len(distinct) <= DISTINCT_LIMIT else []
            ),
        })
    suggested = suggest_mapping(sheet.headers)
    return {
        'columns': columns,
        'rows': len(sheet.rows),
        'suggested': suggested,
        'suggested_types': {
            item['value']: generic_type_key(item['value'])
            for item in (columns[suggested['doc_type']]['distinct'] if suggested['doc_type'] is not None else [])
        },
        'fields': fields_payload(),
    }


def fields_payload() -> list:
    return [{'key': f.key, 'label': f.label, 'required': f.required, 'hint': f.hint} for f in FIELDS]


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

UNIFORM_TYPE_CODES = {
    '300': 'transaction_invoice', '305': 'tax_invoice', '310': 'tax_invoice', '320': 'combined',
    '330': 'credit_invoice', '400': 'receipt', '405': 'receipt',
}

# header_key of a type label -> kogo's type. Checked after the previous software's own labels.
_TYPE_WORDS = {
    'חשבוניתמסקבלה': 'combined', 'חשבוניתקבלה': 'combined', 'חשבוניתמסוקבלה': 'combined',
    'taxinvoicereceipt': 'combined', 'invoicereceipt': 'combined', 'receiptinvoice': 'combined',
    'חשבוניתמס': 'tax_invoice', 'taxinvoice': 'tax_invoice', 'חשבוניתריכוז': 'tax_invoice',
    'קבלה': 'receipt', 'receipt': 'receipt', 'קבלהעלתרומה': 'receipt',
    'חשבוניתעסקה': 'transaction_invoice', 'חשבוןעסקה': 'transaction_invoice',
    'חשבוןעיסקה': 'transaction_invoice', 'חשבוניתעיסקה': 'transaction_invoice',
    'proforma': 'transaction_invoice', 'proformainvoice': 'transaction_invoice',
    'transactioninvoice': 'transaction_invoice', 'dealinvoice': 'transaction_invoice',
    'חשבוניתמסזיכוי': 'credit_invoice', 'חשבוניתזיכוי': 'credit_invoice', 'זיכוי': 'credit_invoice',
    'תעודתזיכוי': 'credit_invoice', 'creditnote': 'credit_invoice', 'creditinvoice': 'credit_invoice',
    'credit': 'credit_invoice', 'refund': 'credit_invoice',
}


def generic_type_key(label) -> str:
    """Any software's name (or מבנה אחיד code) for a document type -> kogo's, or ''."""
    text = clean_text(label)
    if not text:
        return ''
    known = type_key(text)
    if known:
        return known
    if text in UNIFORM_TYPE_CODES:
        return UNIFORM_TYPE_CODES[text]
    key = header_key(text)
    if key in _TYPE_WORDS:
        return _TYPE_WORDS[key]
    # A longer label that says what it is: "חשבונית מס זיכוי מס' 12", "קבלה - העברה".
    if 'זיכוי' in key or 'credit' in key:
        return 'credit_invoice'
    if 'קבלה' in key and 'חשבונית' in key:
        return 'combined'
    if 'עסקה' in key or 'עיסקה' in key:
        return 'transaction_invoice'
    if 'חשבוניתמס' in key:
        return 'tax_invoice'
    if key.startswith('קבלה'):
        return 'receipt'
    return ''


PAYMENT_LABELS = {
    'card': 'כרטיס אשראי', 'cash': 'מזומן', 'check': 'המחאה', 'transfer': 'העברה בנקאית',
    'standing_order': 'הוראת קבע',
}
_PAYMENT_WORDS = (
    ('card', ('אשראי', 'כרטיס', 'credit', 'card', 'visa', 'ויזה', 'מאסטר', 'master', 'amex', 'אמריקן',
              'ישראכרט', 'isracard', 'כאל', 'דיינרס', 'diners')),
    ('standing_order', ('הוראתקבע', 'standingorder', 'directdebit')),
    ('cash', ('מזומן', 'cash')),
    ('check', ('המחאה', 'שיק', 'צק', 'check', 'cheque')),
    ('transfer', ('העברה', 'transfer', 'bank', 'בנק', 'הפקדה', 'wire')),
)


def payment_label(value) -> str:
    """'Visa' -> 'כרטיס אשראי', "צ'ק" -> 'המחאה'; any other way (ביט, PayPal) stays as the file wrote it."""
    text = clean_text(value)
    if text in ('', '-', '—'):
        return ''
    key = header_key(text)
    for kind, words in _PAYMENT_WORDS:
        if any(word in key for word in words):
            return PAYMENT_LABELS[kind]
    return text[:60]


def parse_money(value):
    """A cell as an amount, or None when it is empty or not a number. (100) and 100- are negative."""
    if value is None or value == '' or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(round(float(value), 2))).quantize(Decimal('0.01'))
    text = clean_text(value).replace('₪', '').replace('ש"ח', '').replace('NIS', '').replace(',', '').strip()
    negative = False
    if text.startswith('(') and text.endswith(')'):
        negative, text = True, text[1:-1].strip()
    if text.endswith('-'):
        negative, text = True, text[:-1].strip()
    if text in ('', '-'):
        return None
    try:
        amount = Decimal(text).quantize(Decimal('0.01'))
    except InvalidOperation:
        return None
    return -amount if negative else amount


def read_number(value) -> tuple:
    """(number, printed): 1234 -> (1234, ''); 'INV-0015' -> (15, 'INV-0015'); no digits -> (None, text)."""
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value), ''
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value, ''
    text = clean_text(value)
    if text.isdigit() and int(text) > 0:
        return int(text), '' if str(int(text)) == text else text
    digits = ''.join(_DIGITS.findall(text))
    if digits and int(digits) > 0 and len(digits) <= 18:
        return int(digits), text[:40]
    return None, text


_EXTRA_DATE_FORMATS = ('%Y/%m/%d', '%d.%m.%y', '%Y%m%d', '%d-%m-%y', '%Y.%m.%d')


def read_date(value, datemode: int = 0):
    if isinstance(value, (datetime, date)):
        return parse_date(value, datemode)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return parse_date(value, datemode) if 20000 < value < 80000 else None
    text = clean_text(value).split('T')[0].split(' ')[0]
    found = parse_date(text, datemode)
    if found is not None:
        return found
    for fmt in _EXTRA_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# The table -> rows
# --------------------------------------------------------------------------

def amounts_for(doc_type: str, before_vat, vat, total) -> dict:
    """
    The row's amounts in the columns LegacyDocument has. The total is the file's
    total, else before VAT + VAT. A credit note's amounts are kept positive: its
    type is what makes it a credit (as in the uniform structure).
    """
    if total is None and before_vat is not None:
        total = before_vat + (vat or Decimal('0.00'))
    if before_vat is None and total is not None and vat is not None:
        before_vat = total - vat
    total = total if total is not None else Decimal('0.00')
    if doc_type == 'credit_invoice':
        total = abs(total)
        before_vat = abs(before_vat) if before_vat is not None else None
        vat = abs(vat) if vat is not None else None
    zero = Decimal('0.00')
    return {
        'invoice_total': str(total if doc_type in ('combined', 'tax_invoice', 'transaction_invoice') else zero),
        'receipt_total': str(total if doc_type in ('combined', 'receipt') else zero),
        'credit_total': str(total if doc_type == 'credit_invoice' else zero),
        'withholding': str(zero),
        'before_withholding': str(total),
        'amount_before_vat': str(before_vat) if before_vat is not None else None,
        'vat_amount': str(vat) if vat is not None else None,
    }


def base_row(**values) -> dict:
    """A row with every key parser.parse_sheet's rows have, and the ones only other softwares fill."""
    row = {
        'type_label': '', 'doc_type': '', 'number': None, 'original_number': '', 'date': '',
        'invoice_total': '0.00', 'receipt_total': '0.00', 'credit_total': '0.00', 'withholding': '0.00',
        'before_withholding': '0.00', 'amount_before_vat': None, 'vat_amount': None,
        'status': '', 'payment_type': '', 'card_last_four': '', 'location': '', 'details': '', 'remark': '',
        'allocation_number': '', 'linked_document': '',
        'first_name': '', 'last_name': '', 'email': '', 'phone': '', 'id_number': '', 'ext_number': '',
        'city': '', 'address': '', 'customer_notes': '', 'deleted': False, 'dealer_number': '',
    }
    row.update(values)
    row['customer_key'] = customer_key(row['id_number'], row['ext_number'], row['email'], row['phone'])
    return row


def parse_table(sheet: Sheet, mapping: dict, *, source_system: str, type_values=None,
                fixed_doc_type: str = '') -> tuple:
    """
    (rows, skipped, unknown_types). `type_values` is the office's own answer
    for type labels kogo does not recognise: {label: doc_type}.
    """
    missing = missing_fields(mapping, fixed_doc_type)
    if missing:
        raise ImportFileError('חסר במיפוי: ' + ', '.join(missing))
    overrides = {clean_text(k): v for k, v in (type_values or {}).items() if v in TYPE_LABELS}
    rows, skipped, seen, unknown = [], [], set(), Counter()
    for offset, cells in enumerate(sheet.rows):
        line = offset + 2
        if not any(cell not in (None, '') for cell in cells):
            continue

        def cell(key):
            index = mapping.get(key)
            return cells[index] if index is not None and index < len(cells) else None

        label = clean_text(cell('doc_type'))
        if mapping.get('doc_type') is None:
            doc_type, label = fixed_doc_type, TYPE_LABELS.get(fixed_doc_type, '')
        else:
            doc_type = overrides.get(label) or generic_type_key(label)
        if not doc_type:
            unknown[label or '(ריק)'] += 1
            skipped.append({'row': line, 'reason': f'סוג מסמך לא מוכר: {label or "(ריק)"}'})
            continue
        number, printed = read_number(cell('number'))
        if number is None:
            skipped.append({'row': line, 'reason': 'אין מספר מסמך'})
            continue
        when = read_date(cell('date'), sheet.datemode)
        if when is None:
            skipped.append({'row': line, 'reason': 'אין תאריך'})
            continue
        if (doc_type, number) in seen:
            skipped.append({'row': line, 'reason': 'מסמך כפול בקובץ'})
            continue
        seen.add((doc_type, number))
        customer_number = clean_text(cell('customer_number'))
        rows.append(base_row(
            type_label=(label or TYPE_LABELS[doc_type])[:60],
            doc_type=doc_type,
            number=number,
            original_number=printed,
            date=when.isoformat(),
            **amounts_for(doc_type, parse_money(cell('amount_before_vat')), parse_money(cell('vat')),
                          parse_money(cell('total'))),
            payment_type=payment_label(cell('payment_method')),
            location=clean_text(cell('location'))[:300],
            details=clean_text(cell('details')),
            allocation_number=clean_text(cell('allocation_number'))[:40],
            linked_document=clean_text(cell('linked_document'))[:60],
            first_name=clean_text(cell('customer_name'))[:300],
            email=normalise_email(cell('email')),
            phone=normalise_phone(cell('phone')),
            id_number=normalise_id(cell('customer_id')),
            # Another software's customer numbers are its own: never the previous software's "ext:17".
            ext_number=f'{source_system}:{customer_number}' if customer_number else '',
        ))
    unknown_types = [{'label': label, 'count': count} for label, count in unknown.most_common(50)]
    return rows, skipped, unknown_types
