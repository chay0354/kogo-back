"""The previous software's document export, read into rows kogo can keep.

The export has one row per document the old software issued, and on every row
the details of the customer it was issued to. Nothing here touches the
database: it turns cells into normalised rows (parse_sheet), and rows into
customers and the numbering table (summarise). The preview and the commit both
run on the rows stored with the import, so what the owner confirmed is exactly
what is written.

What is never kept
------------------
The export also carries the customer's password to the old software's app,
their birth date, a fax, a home phone and the extra-phone/SMS flags. Columns
are chosen by name from COLUMNS below, so none of those is ever read out of
the sheet (reader.read_sheet reads only the columns this module asks for) —
not stored with the import, not logged, not returned.

The customer
------------
A customer is keyed by their ת"ז/ח"פ, else the old software's customer number,
else email, else phone. The owner's rule for the details is "the future
definition matters more than history; the latest wins": the name, email, phone
and address are the ones on the customer's newest document.
"""
from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from apps.legacy_import.reader import ImportFileError, Sheet

# field -> the header(s) the old software writes for it. Matched after
# normalise_header, so ״ and " and doubled spaces do not matter.
COLUMNS = {
    'first_name': ('שם פרטי',),
    'last_name': ('שם משפחה',),
    'email': ('אימייל', 'דוא"ל'),
    'phone': ('טלפון נייד',),
    'ext_number': ('מס\' לקוח בהנה"ח חיצונית',),
    'id_number': ('ת"ז \\ ע"מ \\ ח"פ',),
    'city': ('עיר',),
    'address': ('כתובת',),
    'customer_notes': ('הערות',),
    'deleted': ('נמחק',),
    'dealer_number': ('עוסק מורשה',),
    'type_label': ('סוג המסמך',),
    'number': ('מספר המסמך',),
    'details': ('פרטים',),
    'date': ('תאריך',),
    'invoice_total': ('סה"כ ח-ן',),
    'receipt_total': ('סה"כ קבלה',),
    'credit_total': ('סה"כ זיכוי',),
    'remark': ('הערה',),
    'status': ('סטטוס',),
    'payment_type': ('סוג תשלום',),
    'card_last_four': ('מס כרטיס (4 ספרות)',),
    'location': ('מיקום',),
    'withholding': ('סכום הניכוי',),
    'before_withholding': ('סה"כ לפני ניכוי',),
}

# Without these the file is not the documents export (a customers-only export
# has no document number; a report has no customer).
ESSENTIAL = (
    'type_label', 'number', 'date', 'first_name', 'last_name', 'id_number',
    'ext_number', 'location', 'invoice_total', 'receipt_total', 'credit_total',
)

# Named so a test can prove they are never read. Nothing looks them up.
NEVER_READ = (
    'סיסמת כניסה לאפליקציה', 'תאריך לידה', 'פקס', 'טלפון בבית',
    'שימוש בטלפון נוסף', 'שימוש בטלפון נוסף שני',
    'טלפון נוסף 1 לשליחת SMS', 'טלפון נוסף 2 לשליחת SMS',
)

# The old software's type names -> kogo's document types. חשבון עיסקה is its
# spelling of חשבונית עסקה.
DOCUMENT_TYPES = {
    'חשבונית מס קבלה': 'combined',
    'חשבונית מס/קבלה': 'combined',
    'חשבונית מס': 'tax_invoice',
    'חשבונית מס זיכוי': 'credit_invoice',
    'קבלה': 'receipt',
    'חשבון עיסקה': 'transaction_invoice',
    'חשבון עסקה': 'transaction_invoice',
    'חשבונית עסקה': 'transaction_invoice',
    'חשבונית עיסקה': 'transaction_invoice',
}
# The order the numbering table is read in.
TYPE_ORDER = ('combined', 'tax_invoice', 'receipt', 'transaction_invoice', 'credit_invoice')
TYPE_LABELS = {
    'combined': 'חשבונית מס/קבלה',
    'tax_invoice': 'חשבונית מס',
    'receipt': 'קבלה',
    'transaction_invoice': 'חשבונית עסקה',
    'credit_invoice': 'חשבונית מס זיכוי',
}

CARD_PAYMENT = 'כרטיס אשראי'
# A document of any of these is issued by hand, to somebody who is not paying a
# lesson subscription by card.
MANUAL_TYPES = frozenset({'tax_invoice', 'transaction_invoice', 'receipt', 'credit_invoice'})

# A name that is an organisation, not a parent. Matched after the quotes are
# made plain, so מתנ״ס and מתנ"ס are one pattern.
ORGANISATION_NAME = re.compile(
    r'בע"מ|בע״מ|עמותה|עמותת|מתנ"ס|מתנס|עירייה|עיריית|עיריה|מועצה|מועצת|'
    r'בית ספר|בית הספר|ביה"ס|בי"ס|קאנטרי|החברה|מרכז|רשות|משרד|אגודה|אגודת'
)

REASON_LABELS = {
    'document_type': 'הופק לו מסמך ידני (חשבונית מס / עסקה / קבלה / זיכוי)',
    'not_card': 'שילם שלא בכרטיס אשראי',
    'company_number': 'מספר חברה / עמותה (9 ספרות שמתחילות ב-5)',
    'organisation_name': 'השם הוא של ארגון',
    'dealer_number': 'רשום כעוסק מורשה',
}

_QUOTES = str.maketrans({'״': '"', '”': '"', '“': '"', '׳': "'", '’': "'", '‘': "'", '`': "'"})
_SPACES = re.compile(r'\s+')
_NON_DIGITS = re.compile(r'\D+')


# --------------------------------------------------------------------------
# Cells
# --------------------------------------------------------------------------

def normalise_header(value) -> str:
    text = html.unescape(str(value or '')).translate(_QUOTES)
    text = re.sub(r'\s*\\\s*', r' \\ ', text)
    return _SPACES.sub(' ', text).strip()


def clean_text(value) -> str:
    """A cell as text: entities decoded (&#34; -> "), whitespace collapsed."""
    if value is None:
        return ''
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (datetime, date)):
        return value.strftime('%d/%m/%Y')
    return _SPACES.sub(' ', html.unescape(str(value))).strip()


def _dash_is_empty(value) -> str:
    text = clean_text(value)
    return '' if text in ('-', '—', '–') else text


def normalise_id(value) -> str:
    """
    ת"ז / ח"פ as nine digits.

    The export stores the number as a number, so an ID that starts with 0 comes
    back one digit short: 412 of the customers' IDs are eight digits in the
    file. Any all-digit value of five to nine digits is zero-padded back to
    nine. Fewer than five digits, or all zeros, is a placeholder, not an ID.
    """
    text = clean_text(value).replace(' ', '').replace('-', '')
    if not text:
        return ''
    if text.isdigit():
        if len(text) < 5 or not text.strip('0'):
            return ''
        return text.zfill(9) if len(text) <= 9 else text
    return text.upper()


def is_company_number(id_number: str) -> bool:
    """A ח"פ or an amuta's number: nine digits that start with 5."""
    return len(id_number) == 9 and id_number.isdigit() and id_number.startswith('5')


def normalise_phone(value) -> str:
    """Digits only; +972 read as the leading 0, and a mobile that lost its 0 gets it back."""
    digits = _NON_DIGITS.sub('', clean_text(value))
    if digits.startswith('972') and len(digits) >= 11:
        digits = '0' + digits[3:]
    if len(digits) == 9 and digits[0] in '5':
        digits = '0' + digits
    return digits if len(digits) >= 7 else ''


def normalise_email(value) -> str:
    text = clean_text(value).lower().replace(' ', '')
    return text if '@' in text and '.' in text.split('@')[-1] else ''


def parse_amount(value) -> Decimal:
    if value is None or value == '':
        return Decimal('0.00')
    if isinstance(value, (int, float)):
        return Decimal(str(round(float(value), 2))).quantize(Decimal('0.01'))
    text = clean_text(value).replace(',', '').replace('₪', '').strip()
    if text in ('', '-'):
        return Decimal('0.00')
    try:
        return Decimal(text).quantize(Decimal('0.01'))
    except InvalidOperation:
        return Decimal('0.00')


def parse_number(value):
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    text = clean_text(value)
    return int(text) if text.isdigit() and int(text) > 0 else None


def parse_date(value, datemode: int = 0):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and value > 0:
        # A date cell stored as a plain number: Excel's serial day.
        epoch = date(1904, 1, 1) if datemode == 1 else date(1899, 12, 30)
        return epoch + timedelta(days=int(value))
    text = clean_text(value)
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d.%m.%Y', '%d/%m/%y', '%d-%m-%Y'):
        try:
            return datetime.strptime(text.split(' ')[0], fmt).date()
        except ValueError:
            continue
    return None


def parse_card(value) -> str:
    if isinstance(value, float) and value.is_integer():
        digits = str(int(value))
    else:
        digits = _NON_DIGITS.sub('', clean_text(value))
    return digits[-4:].zfill(4) if digits else ''


def type_key(label: str) -> str:
    """'חשבונית מס קבלה' -> 'combined'; '' for a type kogo does not know."""
    text = clean_text(label).translate(_QUOTES)
    return DOCUMENT_TYPES.get(text) or DOCUMENT_TYPES.get(_SPACES.sub(' ', text.replace('/', ' ')).strip(), '')


def customer_key(id_number: str, ext_number: str, email: str, phone: str) -> str:
    """ת"ז/ח"פ, else the old software's customer number, else email, else phone."""
    if id_number:
        return id_number
    if ext_number:
        return f'ext:{ext_number}'
    return email or phone or ''


def plain_name(value: str) -> str:
    """A name for comparing: quotes made plain, spaces collapsed, case folded."""
    return _SPACES.sub(' ', (value or '').translate(_QUOTES)).strip().casefold()


# --------------------------------------------------------------------------
# The sheet -> rows
# --------------------------------------------------------------------------

def column_indexes(headers) -> dict:
    """field -> column index, found by header name. Refuses a file missing the essentials."""
    wanted = {normalise_header(h): name for name, names in COLUMNS.items() for h in names}
    found = {}
    for index, header in enumerate(headers):
        name = wanted.get(normalise_header(header))
        if name and name not in found:
            found[name] = index
    missing = [COLUMNS[name][0] for name in ESSENTIAL if name not in found]
    if missing:
        raise ImportFileError(
            'הקובץ אינו דוח המסמכים של התוכנה הקודמת — חסרות בו העמודות: '
            + ', '.join(missing)
        )
    return found


def parse_sheet(sheet: Sheet) -> tuple[list, list]:
    """
    (rows, skipped). A row is a JSON-ready dict; skipped is [{row, reason}] with
    the spreadsheet's row number and nothing else, so it can be shown and logged.
    """
    columns = column_indexes(sheet.headers)
    rows, skipped, seen = [], [], set()
    for offset, cells in enumerate(sheet.rows):
        line = offset + 2  # the header is row 1
        if not any(cell not in (None, '') for cell in cells):
            continue

        def cell(name):
            index = columns.get(name)
            return cells[index] if index is not None and index < len(cells) else None

        label = clean_text(cell('type_label'))
        doc_type = type_key(label)
        number = parse_number(cell('number'))
        when = parse_date(cell('date'), sheet.datemode)
        if not doc_type:
            skipped.append({'row': line, 'reason': 'סוג מסמך לא מוכר'})
            continue
        if number is None:
            skipped.append({'row': line, 'reason': 'אין מספר מסמך'})
            continue
        if when is None:
            skipped.append({'row': line, 'reason': 'אין תאריך'})
            continue
        if (doc_type, number) in seen:
            skipped.append({'row': line, 'reason': 'מסמך כפול בקובץ'})
            continue
        seen.add((doc_type, number))

        id_number = normalise_id(cell('id_number'))
        ext_number = clean_text(cell('ext_number'))
        email = normalise_email(cell('email'))
        phone = normalise_phone(cell('phone'))
        rows.append({
            'type_label': label,
            'doc_type': doc_type,
            'number': number,
            'date': when.isoformat(),
            'invoice_total': str(parse_amount(cell('invoice_total'))),
            'receipt_total': str(parse_amount(cell('receipt_total'))),
            'credit_total': str(parse_amount(cell('credit_total'))),
            'withholding': str(parse_amount(cell('withholding'))),
            'before_withholding': str(parse_amount(cell('before_withholding'))),
            'status': _dash_is_empty(cell('status')),
            'payment_type': _dash_is_empty(cell('payment_type')),
            'card_last_four': parse_card(cell('card_last_four')),
            'location': clean_text(cell('location')),
            'details': clean_text(cell('details')),
            'remark': _dash_is_empty(cell('remark')),
            'first_name': clean_text(cell('first_name')),
            'last_name': clean_text(cell('last_name')),
            'email': email,
            'phone': phone,
            'id_number': id_number,
            'ext_number': ext_number,
            'city': clean_text(cell('city')),
            'address': clean_text(cell('address')),
            'customer_notes': clean_text(cell('customer_notes')),
            'deleted': clean_text(cell('deleted')) in ('1', 'כן', 'True'),
            'dealer_number': normalise_id(cell('dealer_number')),
            'customer_key': customer_key(id_number, ext_number, email, phone),
        })
    return rows, skipped


def read_rows(content: bytes) -> tuple[list, list]:
    """The export's bytes -> (rows, skipped). Only the columns in COLUMNS are read."""
    from apps.legacy_import.reader import read_sheet

    def keep(headers):
        return set(column_indexes(headers).values())

    return parse_sheet(read_sheet(content, keep=keep))


# --------------------------------------------------------------------------
# Rows -> customers
# --------------------------------------------------------------------------

def _order(row) -> tuple:
    return (row['date'], row['number'])


@dataclass
class Customer:
    key: str
    first_name: str = ''
    last_name: str = ''
    email: str = ''
    phone: str = ''
    id_number: str = ''
    dealer_number: str = ''
    city: str = ''
    address: str = ''
    customer_notes: str = ''
    deleted: bool = False
    ext_numbers: list = field(default_factory=list)
    names: list = field(default_factory=list)  # distinct names, oldest first
    documents: int = 0
    types: dict = field(default_factory=dict)
    latest: dict = field(default_factory=dict)  # the newest document: doc_type, type_label, number, date, location
    kind: str = 'parent'  # 'business' or 'parent'
    reasons: list = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def company_number(self) -> str:
        if is_company_number(self.id_number):
            return self.id_number
        return self.dealer_number if is_company_number(self.dealer_number) else ''

    @property
    def personal_id(self) -> str:
        return '' if is_company_number(self.id_number) else self.id_number

    @property
    def full_address(self) -> str:
        if self.city and self.city not in self.address:
            return ', '.join(part for part in (self.address, self.city) if part)
        return self.address


def classify(customer: Customer, rows: list) -> tuple[str, list]:
    """
    ('business', reasons) or ('parent', []).

    Business/manual: anything the office issued by hand, a חשבונית מס/קבלה paid
    any way but by card, a company number, or an organisation's name. Everyone
    else paid for lessons by card through the old software's subscriptions —
    a parent, who is (or will be) a family in kogo rather than a business
    customer.
    """
    reasons = []
    if any(row['doc_type'] in MANUAL_TYPES for row in rows):
        reasons.append('document_type')
    if any(row['doc_type'] == 'combined' and row['payment_type'] != CARD_PAYMENT for row in rows):
        reasons.append('not_card')
    if is_company_number(customer.id_number):
        reasons.append('company_number')
    if any(ORGANISATION_NAME.search(name.translate(_QUOTES)) for name in customer.names):
        reasons.append('organisation_name')
    if customer.dealer_number:
        reasons.append('dealer_number')
    return ('business', reasons) if reasons else ('parent', [])


def customers_from_rows(rows: list) -> dict:
    """key -> Customer, the latest details winning. Rows without any key belong to nobody."""
    by_key = defaultdict(list)
    for row in rows:
        if row['customer_key']:
            by_key[row['customer_key']].append(row)

    customers = {}
    for key, own in by_key.items():
        own.sort(key=_order)
        customer = Customer(key=key)
        # Oldest to newest: each non-empty value overwrites the one before, so
        # the newest document's details win, and a blank on it does not erase
        # an email the customer had.
        for row in own:
            for name in ('email', 'phone', 'id_number', 'dealer_number', 'city', 'address', 'customer_notes'):
                if row[name]:
                    setattr(customer, name, row[name])
            full = f"{row['first_name']} {row['last_name']}".strip()
            if full and plain_name(full) not in {plain_name(n) for n in customer.names}:
                customer.names.append(full)
            if row['ext_number'] and row['ext_number'] not in customer.ext_numbers:
                customer.ext_numbers.append(row['ext_number'])
        newest = own[-1]
        named = next((row for row in reversed(own) if row['first_name'] or row['last_name']), newest)
        customer.first_name, customer.last_name = named['first_name'], named['last_name']
        # The newest name goes last, even if it was also used before.
        latest_name = customer.full_name
        customer.names = [n for n in customer.names if plain_name(n) != plain_name(latest_name)] + [latest_name]
        customer.deleted = newest['deleted']
        customer.documents = len(own)
        customer.types = dict(Counter(row['doc_type'] for row in own))
        customer.latest = {
            'doc_type': newest['doc_type'],
            'type_label': newest['type_label'],
            'number': newest['number'],
            'date': newest['date'],
            'location': newest['location'],
        }
        customer.kind, customer.reasons = classify(customer, own)
        customers[key] = customer
    return customers


def name_changes(customers: dict) -> list:
    """Customers who appear under more than one name: the old ones, and the one that wins."""
    return sorted(
        (
            {'key': c.key, 'old_names': c.names[:-1], 'new_name': c.names[-1], 'documents': c.documents}
            for c in customers.values() if len(c.names) > 1
        ),
        key=lambda change: change['new_name'],
    )


def type_table(rows: list) -> list:
    """
    Per document type: how many, the first and last number (each with its
    date), the newest date, and how many numbers inside that span the file
    does not have.

    The export is a subset of each of the old software's runs — only the
    documents of the customers it covers — so "missing" is not a gap in the
    old software's numbering; it is what the file cannot show. The last number
    is the last one *this file* has, and kogo only continues a run once the
    owner has confirmed its true last number in the old software.
    """
    by_type = defaultdict(list)
    for row in rows:
        by_type[row['doc_type']].append(row)
    table = []
    for doc_type in TYPE_ORDER:
        own = by_type.get(doc_type)
        if not own:
            continue
        own.sort(key=lambda row: row['number'])
        numbers = {row['number'] for row in own}
        first, last = own[0], own[-1]
        table.append({
            'doc_type': doc_type,
            'label': TYPE_LABELS[doc_type],
            'original_labels': sorted({row['type_label'] for row in own}),
            'count': len(own),
            'first_number': first['number'],
            'first_date': first['date'],
            'last_number': last['number'],
            'last_date': last['date'],
            'latest_date': max(row['date'] for row in own),
            'missing_in_span': (last['number'] - first['number'] + 1) - len(numbers),
        })
    return table


def location_counts(rows: list, customers: dict) -> list:
    """Each distinct location: its documents, and the customers whose newest document is there."""
    documents = Counter(row['location'] for row in rows)
    latest_business = Counter(c.latest['location'] for c in customers.values() if c.kind == 'business')
    latest_all = Counter(c.latest['location'] for c in customers.values())
    return [
        {
            'location': location,
            'documents': count,
            'business_customers': latest_business.get(location, 0),
            'customers': latest_all.get(location, 0),
        }
        for location, count in sorted(documents.items(), key=lambda item: (-item[1], item[0]))
    ]


def details_by_location(rows: list) -> dict:
    """location -> the פרטים of its documents; the places the old software's location field misses."""
    texts = defaultdict(list)
    for row in rows:
        if row['details']:
            texts[row['location']].append(row['details'])
    return texts
