"""מבנה אחיד — the Tax Authority's uniform-structure export: INI.TXT and BKMVDATA.TXT.

Spec: "הוראות להפקת קבצים במבנה אחיד", version 1.31 (רשות המסים, 10.05.2009).
Section numbers in the comments (2.4(יב), 4.3, הבהרה 4 ...) point into it.

Pure by construction: no models, no settings, no database. An adapter turns
FormalDocument rows into the frozen dataclasses below; this module only lays
them out, byte for byte. That keeps every column rule testable without a
database, and keeps the one place that knows column 288 from column 303 free
of ORM concerns.

Documents only. Kogo issues documents and keeps no double-entry books, and the
software is in-house (exempt from registration, נספח ה׳(ג)(1) להוראות ניהול
פנקסי חשבונות). So BKMVDATA.TXT carries A100, C100, D110, D120 and Z900, and
never B100/B110 (ledger) or M100 (inventory).

Amounts are written as given, never derived. `service._compute_totals` rounds
`total_amount` only, so on a rounded document before − discount + VAT does not
always equal the total; recomputing any column here would make the file
disagree with the issued document — the same reasoning as period_report.

Strict where a mistake would falsify the file, lenient where it would only
leave it incomplete. A number that does not fit its column, an unknown code or
a document outside the period raises UniformFormatError. Text is truncated to
its column, and a character the file's charset cannot hold becomes '?': an
emoji in a customer's name must never be the reason an export fails in front
of an auditor.
"""
from __future__ import annotations

import io
import secrets
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from functools import lru_cache
from types import MappingProxyType
from zoneinfo import ZoneInfo

# 2.4(ח): Windows files are logical Hebrew in ISO-8859-8-i. The "-i" (implicit
# directionality) names the logical ordering, not another byte map, so the
# plain ISO-8859-8 codec gives the right bytes as long as text stays in the
# order it was typed — which is why nothing here ever reverses Hebrew.
ENCODING = 'iso-8859-8'
CHARSET_ISO_8859_8_I = 1   # field 1029 (2 would be CP-862, the DOS charset)
LANGUAGE_HEBREW = 0        # field 1028
LEADING_CURRENCY = 'ILS'   # field 1032

# 2.4(ט)(2): every record ends in CR LF, which the record lengths do not count.
RECORD_TERMINATOR = b'\r\n'

SYSTEM_CONSTANT = '&OF1.31&'  # fields 1005, 1104, 1154

# 2.2: <drive>\OPENFRMT\<VAT without check digit>.<YY>\<MMDDhhmm>\, holding
# INI.TXT and BKMVDATA.TXT compressed into an archive named BKMVDATA whose
# extension is the compressor's own (2.2(ד)).
ROOT_DIRECTORY = 'OPENFRMT'
INI_FILENAME = 'INI.TXT'
DATA_FILENAME = 'BKMVDATA.TXT'
ARCHIVE_FILENAME = 'BKMVDATA.zip'
COMPRESSION_SOFTWARE = 'Python zipfile'  # field 1030: what uniform_zip really uses

SOFTWARE_SINGLE_YEAR = 1   # field 1011
SOFTWARE_MULTI_YEAR = 2

# Field 1013. 0 is the spec's own "לא רלוונטי": a system that issues documents
# and keeps no books at all.
ACCOUNTING_NOT_APPLICABLE = 0
ACCOUNTING_SINGLE_ENTRY = 1
ACCOUNTING_DOUBLE_ENTRY = 2

ISRAEL_TIME_ZONE = 'Asia/Jerusalem'

# נספח 1. 2.4(ה): only these codes may appear in a type field.
DOCUMENT_TYPES: Mapping[int, str] = MappingProxyType({
    100: 'הזמנה',
    200: 'תעודת משלוח',
    205: 'תעודת משלוח סוכן',
    210: 'תעודת החזרה',
    300: 'חשבונית / חשבונית עסקה',
    305: 'חשבונית מס',
    310: 'חשבונית ריכוז',
    320: 'חשבונית מס / קבלה',
    330: 'חשבונית מס זיכוי',
    340: 'חשבונית שריון',
    345: 'חשבונית סוכן',
    400: 'קבלה',
    405: 'קבלה על תרומות',
    410: 'יציאה מקופה',
    420: 'הפקדת בנק',
    500: 'הזמנת רכש',
    600: 'תעודת משלוח רכש',
    610: 'החזרת רכש',
    700: 'חשבונית מס רכש',
    710: 'זיכוי רכש',
    800: 'יתרת פתיחה',
    810: 'כניסה כללית למלאי',
    820: 'יציאה כללית מהמלאי',
    830: 'העברה בין מחסנים',
    840: 'עדכון בעקבות ספירה',
    900: 'דוח ייצור - כניסה',
    910: 'דוח ייצור - יציאה',
})

TRANSACTION_INVOICE = 300
TAX_INVOICE = 305
TAX_INVOICE_RECEIPT = 320
CREDIT_INVOICE = 330
RECEIPT = 400

# FormalDocument.document_type -> נספח 1 code, so the adapter does not have to
# re-derive it. Drafts are not documents and have no code on purpose.
DOCUMENT_TYPE_CODES: Mapping[str, int] = MappingProxyType({
    'transaction_invoice': TRANSACTION_INVOICE,
    'tax_invoice': TAX_INVOICE,
    'combined': TAX_INVOICE_RECEIPT,
    'credit_invoice': CREDIT_INVOICE,
    'receipt': RECEIPT,
})

# D120 is "פרטי קבלה / הפקדה" — money received or deposited. Only these types
# record it; payment rows on a tax invoice mean the adapter mixed documents up.
PAYMENT_DOCUMENT_TYPES = frozenset({320, 400, 405, 410, 420})

# Field 1306. The first four keys are FormalDocument's PAYMENT_METHOD_CHOICES.
PAYMENT_METHOD_CODES: Mapping[str, int] = MappingProxyType({
    'cash': 1,
    'check': 2,
    'credit_card': 3,
    'bank_transfer': 4,
    'voucher': 5,          # תווי קניה
    'exchange_slip': 6,    # תלוש החלפה
    'promissory_note': 7,  # שטר
    'standing_order': 8,   # הוראת קבע
    'other': 9,
})
_CHECK = PAYMENT_METHOD_CODES['check']
_CREDIT_CARD = PAYMENT_METHOD_CODES['credit_card']

# Field 1313. The spec itself skips 5.
CARD_ACQUIRERS: Mapping[int, str] = MappingProxyType({
    1: 'ישראכרט',
    2: 'כאל',
    3: 'דיינרס',
    4: 'אמריקן אקספרס',
    6: 'לאומי כארד',
})

# Field 1315.
CARD_REGULAR = 1
CARD_INSTALLMENTS = 2
CARD_CREDIT = 3
CARD_DEFERRED = 4
CARD_OTHER = 5

# Field 1258.
TRANSACTION_SERVICE = 1
TRANSACTION_SALE = 2
TRANSACTION_SERVICE_AND_SALE = 3

# Field 1263: a meaningful unit (ליטר, שעת עבודה) by name, "otherwise the word יחידה".
DEFAULT_UNIT = 'יחידה'

# 2.5(ה). 'SUMMARY' is the 19-character INI line of 3.2, one per record code.
RECORD_LENGTHS: Mapping[str, int] = MappingProxyType({
    'A000': 466,
    'SUMMARY': 19,
    'A100': 95,
    'C100': 444,
    'D110': 339,
    'D120': 222,
    'Z900': 110,
})


class UniformFormatError(ValueError):
    """Input the uniform structure cannot hold: an overflowing number, an unknown code, a stray document."""


def _require_date(value: object, label: str, *, optional: bool = False) -> None:
    # datetime is a date subclass, but one would compare badly against the
    # period and hide a time-zone choice; the caller must make that choice.
    if value is None and optional:
        return
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f'{label}: expected a date, got {type(value).__name__}')


def _require_code(value: object, allowed: Mapping[int, object] | frozenset, label: str,
                  *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        raise UniformFormatError(f'{label}: {value!r} is not one of the spec\'s codes {sorted(allowed)}')


@dataclass(frozen=True, kw_only=True)
class UniformAddress:
    """Street, house number, city and zip — the four address columns A000 and C100 share."""
    street: str = ''
    house_number: str = ''
    city: str = ''
    zip_code: str = ''


@dataclass(frozen=True, kw_only=True)
class UniformBusiness:
    """
    The business the export is for (A000, and the VAT number on every record).

    The software is in-house, so its vendor is the business itself: the vendor
    fields default to the business's own number and name, and the registration
    certificate number is 0 because an exempt program has none.
    """
    vat_number: str                              # 1003 and every record's עוסק מורשה field
    name: str                                    # 1018
    software_version: str                        # 1008
    address: UniformAddress = UniformAddress()   # 1019–1022
    company_number: str = ''                     # 1015 ח.פ.
    withholding_file_number: str = ''            # 1016 תיק ניכויים
    software_name: str = 'Kogo CRM'              # 1007
    software_registration_number: int = 0        # 1006
    vendor_vat_number: str = ''                  # 1009; empty means the business itself
    vendor_name: str = ''                        # 1010; empty means the business itself
    software_type: int = SOFTWARE_MULTI_YEAR     # 1011
    accounting_type: int = ACCOUNTING_NOT_APPLICABLE  # 1013
    # 1034 and הבהרה 3: branches that keep books of their own, not studio
    # locations. When True every document must name its branch.
    has_branches: bool = False

    def __post_init__(self) -> None:
        _require_code(self.software_type, frozenset({SOFTWARE_SINGLE_YEAR, SOFTWARE_MULTI_YEAR}),
                      'A000 field 1011')
        _require_code(self.accounting_type, frozenset({0, 1, 2}), 'A000 field 1013')


@dataclass(frozen=True, kw_only=True)
class UniformLine:
    """
    One D110 line. Amounts are before VAT and in shekels (1265: "סכום בש"ח").

    `line_discount` is the discount as printed, a positive number; the file
    shows it negative because it reduces the line (2.4(יב), הבהרה 5).
    """
    description: str                        # 1260
    quantity: Decimal                       # 1264
    unit_price: Decimal                     # 1265
    line_total: Decimal                     # 1267: quantity × unit price, less the line discount
    vat_rate: Decimal                       # 1268, a percent: Decimal('18'); 0 on an exempt document
    line_discount: Decimal = Decimal('0')   # 1266
    catalog_number: str = ''                # 1259
    unit_of_measure: str = DEFAULT_UNIT     # 1263
    transaction_type: int | None = None     # 1258: TRANSACTION_SERVICE / _SALE / _SERVICE_AND_SALE

    def __post_init__(self) -> None:
        _require_code(self.transaction_type, frozenset({1, 2, 3}), 'D110 field 1258', optional=True)


@dataclass(frozen=True, kw_only=True)
class UniformPayment:
    """
    One D120 row: one means of payment on a receipt or an invoice-receipt.

    The bank columns (1307–1310) are "בהמחאה בלבד" and the date (1311) is "in
    a check or a credit card only", so they are written for those methods and
    left zero for the rest. The card columns (1313–1315) go with cards only.
    """
    method: str                             # a PAYMENT_METHOD_CODES key -> 1306
    amount: Decimal                         # 1312
    due_date: date | None = None            # 1311: a check's due date, a card's charge date
    bank_number: str = ''                   # 1307 — digits, not a bank's name
    branch_number: str = ''                 # 1308
    account_number: str = ''                # 1309
    check_number: str = ''                  # 1310
    card_acquirer: int | None = None        # 1313, a CARD_ACQUIRERS key
    card_name: str = ''                     # 1314
    card_transaction_type: int | None = None  # 1315: CARD_REGULAR, CARD_INSTALLMENTS ...

    def __post_init__(self) -> None:
        if self.method not in PAYMENT_METHOD_CODES:
            raise UniformFormatError(
                f'D120 field 1306: unknown payment method {self.method!r}; use one of {sorted(PAYMENT_METHOD_CODES)}'
            )
        _require_date(self.due_date, 'D120 field 1311', optional=True)
        _require_code(self.card_acquirer, CARD_ACQUIRERS, 'D120 field 1313', optional=True)
        _require_code(self.card_transaction_type, frozenset({1, 2, 3, 4, 5}), 'D120 field 1315', optional=True)


@dataclass(frozen=True, kw_only=True)
class UniformDocument:
    """
    One issued document: its C100 header, D110 lines and D120 payments.

    Dates. `issue_date`/`issue_time` are when the system produced it (1205,
    1206); `document_date` is the date printed on it (1230), which defaults to
    the issue date and is the date the period is cut by (2.1(ג), הבהרה 12).
    Times are written as given, so pass Israel local time.

    Amounts are the stored ones, in shekels, and positive — a credit note too:
    its type (330) is what makes it reduce income, not a sign (2.4(יב),
    הבהרה 1). A negative amount is written negative and, per נספח 1, reverses
    the document's effect. `discount` is the discount as printed (positive);
    the file shows it negative. On a receipt, הבהרה 4 puts the amount received
    in 1219, 1221 and 1223 and zero in the discount and VAT.

    `linked_document_*` names the document this one is based on — for a
    credit note, the invoice it credits. The spec carries it on the lines
    (1256/1257), so a document without lines cannot show its link.
    """
    type_code: int                          # 1203, a DOCUMENT_TYPES key
    number: str                             # 1204, exactly as printed
    issue_date: date                        # 1205
    issue_time: time | None = None          # 1206
    document_date: date | None = None       # 1230
    value_date: date | None = None          # 1216 (e.g. the payment due date)
    customer_name: str                      # 1207
    customer_vat_number: str = ''           # 1215, digits only
    customer_address: UniformAddress = UniformAddress()  # 1208–1211
    customer_phone: str = ''                # 1214
    customer_key: str = ''                  # 1225: the customer's id in Kogo
    amount_before_discount: Decimal         # 1219
    discount: Decimal = Decimal('0')        # 1220
    amount_after_discount: Decimal          # 1221, before VAT
    vat_amount: Decimal                     # 1222
    total_amount: Decimal                   # 1223, including VAT
    withholding_tax: Decimal = Decimal('0')  # 1224 (receipts only)
    foreign_currency_code: str = ''         # 1218 (export invoices only)
    foreign_currency_total: Decimal | None = None  # 1217
    cancelled: bool = False                 # 1228
    linked_document_type: int | None = None  # 1256
    linked_document_number: str = ''        # 1257
    branch_id: str = ''                     # 1231, 1270, 1320 — a short code, 7 characters
    issued_by: str = ''                     # 1233
    lines: tuple[UniformLine, ...] = ()
    payments: tuple[UniformPayment, ...] = ()

    def __post_init__(self) -> None:
        # Lists become tuples so the record really is immutable.
        object.__setattr__(self, 'lines', tuple(self.lines))
        object.__setattr__(self, 'payments', tuple(self.payments))
        label = f'document {self.type_code!r} {self.number!r}'
        _require_code(self.type_code, DOCUMENT_TYPES, f'C100 field 1203 ({label})')
        if not isinstance(self.number, str) or not self.number.strip():
            raise UniformFormatError(f'C100 field 1204 ({label}): a document needs its number')
        _require_date(self.issue_date, f'C100 field 1205 ({label})')
        _require_date(self.document_date, f'C100 field 1230 ({label})', optional=True)
        _require_date(self.value_date, f'C100 field 1216 ({label})', optional=True)
        if self.issue_time is not None and not isinstance(self.issue_time, time):
            raise TypeError(f'C100 field 1206 ({label}): expected a time')
        has_linked_number = bool(self.linked_document_number.strip())
        if (self.linked_document_type is None) == has_linked_number:
            raise UniformFormatError(
                f'D110 fields 1256/1257 ({label}): a base document needs both its type and its number'
            )
        _require_code(self.linked_document_type, DOCUMENT_TYPES, f'D110 field 1256 ({label})', optional=True)
        if bool(self.foreign_currency_code.strip()) != (self.foreign_currency_total is not None):
            raise UniformFormatError(
                f'C100 fields 1217/1218 ({label}): a foreign-currency total needs its currency code, and back'
            )
        if self.payments and self.type_code not in PAYMENT_DOCUMENT_TYPES:
            raise UniformFormatError(
                f'D120 ({label}): only receipts and invoice-receipts record payments '
                f'({sorted(PAYMENT_DOCUMENT_TYPES)})'
            )

    @property
    def date_on_document(self) -> date:
        """The date printed on the document (1230) — what the period is cut by."""
        return self.document_date or self.issue_date


@dataclass(frozen=True)
class UniformFiles:
    """The two files, ready to write, and the figures an adapter shows the user (נספח 4)."""
    ini: bytes
    bkmvdata: bytes
    counts: Mapping[str, int]   # BKMVDATA records per code: {'A100': 1, 'C100': 4, ...}
    primary_id: int             # 1004 = 1103 = 1153
    directory: str              # 'OPENFRMT/51650441.26/09111230' — where the files belong

    @property
    def total_records(self) -> int:
        """Every BKMVDATA record, opening and closing included (1002 = 1155)."""
        return sum(self.counts.values())


# --- Field helpers ------------------------------------------------------------
#
# 2.3(ה): a numeric field is right-aligned and zero-filled, an alphanumeric one
# left-aligned and space-filled; an empty optional field is zeros or spaces
# accordingly (2.3(ז)). Numbers are never cut — a figure that does not fit its
# column raises. Text is cut to its column.

# Characters the charset lacks that have an obvious stand-in. Anything else it
# cannot hold becomes '?' (see _charset_char).
_REPLACEMENTS: Mapping[str, str] = MappingProxyType({
    '׳': "'",      # geresh
    '״': '"',      # gershayim, as in בע״מ
    '־': '-',      # maqaf
    '׀': '|',      # paseq
    '׃': ':',      # sof pasuq
    'װ': 'וו',
    'ױ': 'וי',
    'ײ': 'יי',
    '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-', '―': '-', '−': '-',
    '‘': "'", '’': "'", '‚': "'", '‛': "'", '′': "'",
    '“': '"', '”': '"', '„': '"', '‟': '"', '″': '"',
    '•': '*',
    '…': '...',
    '₪': 'ש"ח',
    '€': 'EUR',
    ' ': ' ',      # in the charset, but a plain space is what was meant
})
_INVISIBLE = frozenset({'Mn', 'Me', 'Cf'})


def _encodable(ch: str) -> bool:
    try:
        ch.encode(ENCODING)
    except UnicodeEncodeError:
        return False
    return True


@lru_cache(maxsize=4096)
def _charset_char(ch: str) -> str:
    if ch in _REPLACEMENTS:
        return _REPLACEMENTS[ch]
    category = unicodedata.category(ch)
    if category in ('Cc', 'Zl', 'Zp'):
        # A CR or LF inside a field would end the record early (2.4(ט)(2)).
        return ' '
    if category in _INVISIBLE:
        # Niqqud, cantillation, emoji variation selectors, bidi and zero-width
        # marks: invisible, and logical Hebrew needs no direction marks.
        return ''
    if _encodable(ch):
        return ch
    # Compatibility forms with a plain equivalent: 'é' -> 'e', 'שׁ' -> 'ש', 'Ａ' -> 'A'.
    plain = ''.join(c for c in unicodedata.normalize('NFKD', ch) if unicodedata.category(c) not in _INVISIBLE)
    if plain and all(_encodable(c) for c in plain):
        return plain
    return '?'


def to_charset(text: str) -> str:
    """`text` keeping every character the file's charset holds, with a stand-in for the rest."""
    return ''.join(_charset_char(ch) for ch in text)


def text_field(value: object, width: int) -> str:
    """X(n): charset-safe, left-aligned, space-filled, cut to the column."""
    text = '' if value is None else to_charset(str(value)).strip()
    return text[:width].ljust(width)


def identifier_field(value: object, width: int, label: str = '') -> str:
    """
    X(n) for a value other records must repeat exactly — a document number,
    which 2.4(ד) wants identical in C100, D110 and D120. Cut or altered, it
    would name a different document, so this raises where text_field cuts.
    """
    raw = '' if value is None else str(value).strip()
    if to_charset(raw) != raw:
        raise UniformFormatError(f'{label}: {raw!r} has characters {ENCODING} cannot hold')
    if len(raw) > width:
        raise UniformFormatError(f'{label}: {raw!r} is longer than its {width} characters')
    return raw.ljust(width)


# Identifiers arrive formatted ('51-650441-2', '12-345'); the separators are not the number.
_SEPARATORS = str.maketrans('', '', ' -/')


def numeric_field(value: int | str | None, width: int, label: str = '') -> str:
    """9(n): digits only, right-aligned, zero-filled. A value that does not fit raises."""
    if value is None:
        return '0' * width
    if isinstance(value, bool):
        raise TypeError(f'{label}: expected a number, got a bool')
    if isinstance(value, int):
        if value < 0:
            raise UniformFormatError(f'{label}: {value} is negative; 9({width}) has no sign')
        digits = str(value)
    elif isinstance(value, str):
        digits = value.strip().translate(_SEPARATORS)
        if not digits:
            return '0' * width
        if not (digits.isascii() and digits.isdigit()):
            raise UniformFormatError(f'{label}: {value!r} is not a number')
        # Leading zeros are padding, not part of the value.
        digits = digits.lstrip('0') or '0'
    else:
        raise TypeError(f'{label}: expected an int or a digit string, got {type(value).__name__}')
    if len(digits) > width:
        raise UniformFormatError(f'{label}: {value!r} does not fit 9({width})')
    return digits.zfill(width)


def _decimal(value: object, label: str) -> Decimal:
    # A float would carry binary noise into a tax file; amounts come as Decimal.
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f'{label}: amounts are Decimal or int, got {type(value).__name__}')
    number = Decimal(value)
    if not number.is_finite():
        raise UniformFormatError(f'{label}: {value} is not a finite amount')
    return number


def _scaled(number: Decimal, decimals: int, digits: int, label: str, picture: str) -> int:
    """`number` as an integer count of its last decimal place, rounded half-up; overflow raises."""
    with localcontext() as context:
        context.prec = 64
        try:
            scaled = int((number * 10 ** decimals).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        except InvalidOperation:
            scaled = None
    if scaled is None or abs(scaled) >= 10 ** digits:
        raise UniformFormatError(f'{label}: {number} does not fit {picture}')
    return scaled


def amount_field(value: Decimal | int | None, width: int, decimals: int = 2, label: str = '') -> str:
    """
    X9(n)v99: a sign, then digits with an implied decimal point (2.3(ו)).

    `width` counts the sign, as the spec's lengths do: X9(12)v99 is 15 wide.
    The spec's own examples in X9(5)v99: -12345.65 is "-1234565", 1245.65 is
    "+0124565", 1245 is "+0124500". Zero is "+" (2.4(יא)). Extra decimals are
    rounded half-up; the whole part is never cut. None means "not applicable"
    and is spaces, as an unfilled alphanumeric field is (2.3(ז)).
    """
    if value is None:
        return ' ' * width
    number = _decimal(value, label)
    digits = width - 1
    scaled = _scaled(number, decimals, digits, label, f'X9({digits - decimals})v{"9" * decimals}')
    return ('-' if scaled < 0 else '+') + str(abs(scaled)).zfill(digits)


def rate_field(value: Decimal | int | None, width: int, decimals: int = 2, label: str = '') -> str:
    """9(n)v99 with no sign — the line VAT rate, where 18% is "1800" (1268: "(1550)")."""
    if value is None:
        return '0' * width
    number = _decimal(value, label)
    if number < 0:
        raise UniformFormatError(f'{label}: {number} is negative; 9({width - decimals})v99 has no sign')
    return str(_scaled(number, decimals, width, label, f'9({width - decimals})v{"9" * decimals}')).zfill(width)


def date_field(value: date | None) -> str:
    """YYYYMMDD (2.4(ב)); zeros when there is none."""
    if value is None:
        return '0' * 8
    return f'{value.year:04d}{value.month:02d}{value.day:02d}'


def time_field(value: time | datetime | None) -> str:
    """hhmm, 24-hour (2.4(ג)); zeros when there is none."""
    if value is None:
        return '0' * 4
    return f'{value.hour:02d}{value.minute:02d}'


def _reduction(value: object, label: str) -> Decimal:
    """A discount as printed (positive), negated: it reduces the amount (2.4(יב), הבהרה 5)."""
    number = _decimal(value, label)
    if number < 0:
        raise UniformFormatError(f'{label}: pass the discount as printed, a positive number; the file negates it')
    return -number


# --- Record layouts -------------------------------------------------------------
#
# (field number, first column, width, kind), transcribed from the tables of
# sections 3.1, 3.2 and 4.1–4.5. Cancelled fields ("שדה מבוטל!", X(0)) take no
# columns and are left out. Kinds: X text, I identifier, N 9(n), D date, T time,
# A2/A4 a signed amount with 2/4 decimals, P2 an unsigned 9(n)v99.
LAYOUTS: Mapping[str, tuple[tuple[int, int, int, str], ...]] = MappingProxyType({
    'A000': (
        (1000, 1, 4, 'X'), (1001, 5, 5, 'X'), (1002, 10, 15, 'N'), (1003, 25, 9, 'N'),
        (1004, 34, 15, 'N'), (1005, 49, 8, 'X'), (1006, 57, 8, 'N'), (1007, 65, 20, 'X'),
        (1008, 85, 20, 'X'), (1009, 105, 9, 'N'), (1010, 114, 20, 'X'), (1011, 134, 1, 'N'),
        (1012, 135, 50, 'X'), (1013, 185, 1, 'N'), (1014, 186, 1, 'N'), (1015, 187, 9, 'N'),
        (1016, 196, 9, 'N'), (1017, 205, 10, 'X'), (1018, 215, 50, 'X'), (1019, 265, 50, 'X'),
        (1020, 315, 10, 'X'), (1021, 325, 30, 'X'), (1022, 355, 8, 'X'), (1023, 363, 4, 'N'),
        (1024, 367, 8, 'D'), (1025, 375, 8, 'D'), (1026, 383, 8, 'D'), (1027, 391, 4, 'T'),
        (1028, 395, 1, 'N'), (1029, 396, 1, 'N'), (1030, 397, 20, 'X'),
        (1032, 417, 3, 'X'), (1034, 420, 1, 'N'), (1035, 421, 46, 'X'),
    ),
    'SUMMARY': (
        (1050, 1, 4, 'X'), (1051, 5, 15, 'N'),
    ),
    'A100': (
        (1100, 1, 4, 'X'), (1101, 5, 9, 'N'), (1102, 14, 9, 'N'), (1103, 23, 15, 'N'),
        (1104, 38, 8, 'X'), (1105, 46, 50, 'X'),
    ),
    'C100': (
        (1200, 1, 4, 'X'), (1201, 5, 9, 'N'), (1202, 14, 9, 'N'), (1203, 23, 3, 'N'),
        (1204, 26, 20, 'I'), (1205, 46, 8, 'D'), (1206, 54, 4, 'T'), (1207, 58, 50, 'X'),
        (1208, 108, 50, 'X'), (1209, 158, 10, 'X'), (1210, 168, 30, 'X'), (1211, 198, 8, 'X'),
        (1212, 206, 30, 'X'), (1213, 236, 2, 'X'), (1214, 238, 15, 'X'), (1215, 253, 9, 'N'),
        (1216, 262, 8, 'D'), (1217, 270, 15, 'A2'), (1218, 285, 3, 'X'), (1219, 288, 15, 'A2'),
        (1220, 303, 15, 'A2'), (1221, 318, 15, 'A2'), (1222, 333, 15, 'A2'), (1223, 348, 15, 'A2'),
        (1224, 363, 12, 'A2'), (1225, 375, 15, 'X'), (1226, 390, 10, 'X'), (1228, 400, 1, 'X'),
        (1230, 401, 8, 'D'), (1231, 409, 7, 'X'), (1233, 416, 9, 'X'), (1234, 425, 7, 'N'),
        (1235, 432, 13, 'X'),
    ),
    'D110': (
        (1250, 1, 4, 'X'), (1251, 5, 9, 'N'), (1252, 14, 9, 'N'), (1253, 23, 3, 'N'),
        (1254, 26, 20, 'I'), (1255, 46, 4, 'N'), (1256, 50, 3, 'N'), (1257, 53, 20, 'I'),
        (1258, 73, 1, 'N'), (1259, 74, 20, 'X'), (1260, 94, 30, 'X'), (1261, 124, 50, 'X'),
        (1262, 174, 30, 'X'), (1263, 204, 20, 'X'), (1264, 224, 17, 'A4'), (1265, 241, 15, 'A2'),
        (1266, 256, 15, 'A2'), (1267, 271, 15, 'A2'), (1268, 286, 4, 'P2'), (1270, 290, 7, 'X'),
        (1272, 297, 8, 'D'), (1273, 305, 7, 'N'), (1274, 312, 7, 'X'), (1275, 319, 21, 'X'),
    ),
    'D120': (
        (1300, 1, 4, 'X'), (1301, 5, 9, 'N'), (1302, 14, 9, 'N'), (1303, 23, 3, 'N'),
        (1304, 26, 20, 'I'), (1305, 46, 4, 'N'), (1306, 50, 1, 'N'), (1307, 51, 10, 'N'),
        (1308, 61, 10, 'N'), (1309, 71, 15, 'N'), (1310, 86, 10, 'N'), (1311, 96, 8, 'D'),
        (1312, 104, 15, 'A2'), (1313, 119, 1, 'N'), (1314, 120, 20, 'X'), (1315, 140, 1, 'N'),
        (1320, 141, 7, 'X'), (1322, 148, 8, 'D'), (1323, 156, 7, 'N'), (1324, 163, 60, 'X'),
    ),
    'Z900': (
        (1150, 1, 4, 'X'), (1151, 5, 9, 'N'), (1152, 14, 9, 'N'), (1153, 23, 15, 'N'),
        (1154, 38, 8, 'X'), (1155, 46, 15, 'N'), (1156, 61, 50, 'X'),
    ),
})

_FORMATTERS = {
    'X': lambda value, width, label: text_field(value, width),
    'I': identifier_field,
    'N': numeric_field,
    'D': lambda value, width, label: date_field(value),
    'T': lambda value, width, label: time_field(value),
    'A2': lambda value, width, label: amount_field(value, width, 2, label),
    'A4': lambda value, width, label: amount_field(value, width, 4, label),
    'P2': lambda value, width, label: rate_field(value, width, 2, label),
}


def _assemble(code: str, values: Mapping[int, object], context: str = '') -> str:
    """One record, field by field, checked against its layout and the spec's record length."""
    layout = LAYOUTS[code]
    unknown = set(values) - {row[0] for row in layout}
    if unknown:
        raise AssertionError(f'{code} has no field {sorted(unknown)}')
    suffix = f' ({context})' if context else ''
    parts = []
    column = 1
    for number, start, width, kind in layout:
        if start != column:
            raise AssertionError(f'{code} field {number} starts at {start}, not {column}')
        text = _FORMATTERS[kind](values.get(number), width, f'{code} field {number}{suffix}')
        if len(text) != width:
            raise AssertionError(f'{code} field {number} came out {len(text)} wide, not {width}')
        parts.append(text)
        column += width
    if column - 1 != RECORD_LENGTHS[code]:
        raise AssertionError(f'{code} is {column - 1} characters; the spec says {RECORD_LENGTHS[code]}')
    return ''.join(parts)


def _encode(line: str) -> bytes:
    # Strict on purpose: every field went through to_charset, so a failure is a bug here.
    return line.encode(ENCODING) + RECORD_TERMINATOR


# --- Building the files ---------------------------------------------------------

# The first field of each BKMVDATA record; the next two are always its running
# number and the business's VAT number.
_FIRST_FIELD = {'A100': 1100, 'C100': 1200, 'D110': 1250, 'D120': 1300, 'Z900': 1150}

# The order נספח 4 lists record codes in.
_SUMMARY_ORDER = ('A100', 'B100', 'B110', 'C100', 'D110', 'D120', 'M100', 'Z900')


def new_primary_id() -> int:
    """A fresh random 15-digit number (2.4(א), הבהרה 2): a different one for every export."""
    return 10 ** 14 + secrets.randbelow(9 * 10 ** 14)


def israel_wall_clock(moment: datetime) -> datetime:
    """`moment` on the Israeli clock; a naive value is taken as already local."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment
    return moment.astimezone(ZoneInfo(ISRAEL_TIME_ZONE))


def export_directory(vat_number: str, generated_at: datetime) -> str:
    """
    OPENFRMT/<the VAT number's first eight digits, without the check digit>.<YY>/<MMDDhhmm>
    of the export's moment (2.2(א)–(ג)). The audited year is not in the path;
    it lives in INI.TXT.
    """
    vat = numeric_field(vat_number, 9, 'business VAT number')
    moment = israel_wall_clock(generated_at)
    return f'{ROOT_DIRECTORY}/{vat[:8]}.{moment.year % 100:02d}/{moment:%m%d%H%M}'


class _DataWriter:
    """BKMVDATA.TXT as it is written: records numbered in order, and a count per code."""

    def __init__(self, vat_number: str) -> None:
        self.vat_number = vat_number
        self.records: list[bytes] = []
        self.counts: dict[str, int] = {}

    @property
    def next_number(self) -> int:
        return len(self.records) + 1

    def write(self, code: str, values: dict[int, object], context: str = '') -> None:
        # 2.4(י): every record opens with its code, its running number in the
        # file and the business's VAT number.
        first = _FIRST_FIELD[code]
        record = {first: code, first + 1: self.next_number, first + 2: self.vat_number, **values}
        self.records.append(_encode(_assemble(code, record, context)))
        self.counts[code] = self.counts.get(code, 0) + 1


def _check_placement(doc: UniformDocument, business: UniformBusiness,
                     period_start: date, period_end: date, seen: set) -> None:
    label = f'document {doc.type_code} {doc.number}'
    on_document = doc.date_on_document
    if not period_start <= on_document <= period_end:
        raise UniformFormatError(
            f'{label} is dated {on_document}, outside {period_start}..{period_end}; '
            f'documents are cut by their date (2.1(ג), הבהרה 12)'
        )
    key = (doc.type_code, doc.number.strip())
    if key in seen:
        raise UniformFormatError(f'{label} appears twice; its number is how its records find it (2.4(ד))')
    seen.add(key)
    if business.has_branches and not doc.branch_id.strip():
        raise UniformFormatError(f'{label}: the business has branches (1034 = 1), so it must name one (הבהרה 3)')


def _write_document(writer: _DataWriter, doc: UniformDocument, link: int) -> None:
    context = f'document {doc.type_code} {doc.number}'
    on_document = doc.date_on_document
    address = doc.customer_address
    linked = bool(doc.linked_document_number.strip())
    writer.write('C100', {
        1203: doc.type_code,
        1204: doc.number,
        1205: doc.issue_date,
        1206: doc.issue_time,
        1207: doc.customer_name,
        1208: address.street,
        1209: address.house_number,
        1210: address.city,
        1211: address.zip_code,
        1214: doc.customer_phone,
        1215: doc.customer_vat_number,
        1216: doc.value_date,
        # "ימולא רק בחשבונית ייצוא": on any other document these stay unfilled,
        # which for an alphanumeric column is spaces (2.3(ז)).
        1217: doc.foreign_currency_total,
        1218: doc.foreign_currency_code,
        1219: doc.amount_before_discount,
        1220: _reduction(doc.discount, f'C100 field 1220 ({context})'),
        1221: doc.amount_after_discount,
        1222: doc.vat_amount,
        1223: doc.total_amount,
        1224: doc.withholding_tax,
        1225: doc.customer_key,
        1228: '1' if doc.cancelled else '',
        1230: on_document,
        1231: doc.branch_id,
        1233: doc.issued_by,
        # הבהרה 11: the internal number tying a header to its lines. The
        # document's position in this export is unique in the file, which is
        # all the link must be, and always fits 9(7) — a UUID never would.
        1234: link,
    }, context)
    for number, line in enumerate(doc.lines, start=1):
        line_context = f'{context} line {number}'
        writer.write('D110', {
            1253: doc.type_code,
            1254: doc.number,
            1255: number,
            1256: doc.linked_document_type,
            1257: doc.linked_document_number,
            1258: line.transaction_type,
            1259: line.catalog_number,
            1260: line.description,
            1263: (line.unit_of_measure or '').strip() or DEFAULT_UNIT,
            1264: line.quantity,
            1265: line.unit_price,
            1266: _reduction(line.line_discount, f'D110 field 1266 ({line_context})'),
            1267: line.line_total,
            1268: line.vat_rate,
            1270: doc.branch_id,
            1272: on_document,
            1273: link,
            # The base document's branch. Kogo credits its own invoices and its
            # series run business-wide, so the base shares this document's branch.
            1274: doc.branch_id if linked else '',
        }, line_context)
    for number, payment in enumerate(doc.payments, start=1):
        method = PAYMENT_METHOD_CODES[payment.method]
        is_check = method == _CHECK
        is_card = method == _CREDIT_CARD
        writer.write('D120', {
            1303: doc.type_code,
            1304: doc.number,
            1305: number,
            1306: method,
            1307: payment.bank_number if is_check else None,
            1308: payment.branch_number if is_check else None,
            1309: payment.account_number if is_check else None,
            1310: payment.check_number if is_check else None,
            1311: payment.due_date if is_check or is_card else None,
            1312: payment.amount,
            1313: payment.card_acquirer if is_card else None,
            1314: payment.card_name if is_card else '',
            1315: payment.card_transaction_type if is_card else None,
            1320: doc.branch_id,
            1322: on_document,
            1323: link,
        }, f'{context} payment {number}')


def _ini(business: UniformBusiness, vat: str, primary_id: int, counts: Mapping[str, int],
         period_start: date, period_end: date, moment: datetime, directory: str) -> bytes:
    multi_year = business.software_type == SOFTWARE_MULTI_YEAR
    address = business.address
    header = _assemble('A000', {
        1000: 'A000',
        1002: sum(counts.values()),
        1003: vat,
        1004: primary_id,
        1005: SYSTEM_CONSTANT,
        1006: business.software_registration_number,
        1007: business.software_name,
        1008: business.software_version,
        1009: business.vendor_vat_number or vat,
        1010: business.vendor_name or business.name,
        1011: business.software_type,
        # The spec's example is a Windows path from a drive root. The files leave
        # the server as a ZIP, so the drive is the user's choice and is not named.
        1012: directory.replace('/', '\\'),
        1013: business.accounting_type,
        1014: None,  # 1 or 2 only under double-entry books
        1015: business.company_number,
        1016: business.withholding_file_number,
        1018: business.name,
        1019: address.street,
        1020: address.house_number,
        1021: address.city,
        1022: address.zip_code,
        1023: None if multi_year else period_start.year,
        1024: period_start if multi_year else None,
        1025: period_end if multi_year else None,
        1026: moment.date(),
        1027: moment,
        1028: LANGUAGE_HEBREW,
        1029: CHARSET_ISO_8859_8_I,
        1030: COMPRESSION_SOFTWARE,
        1032: LEADING_CURRENCY,
        1034: 1 if business.has_branches else 0,
    })
    # 2.5(ב) and 3.2: a summary "for every record type in BKMVDATA.TXT". The
    # opening and closing records are record types in it too, so they get a
    # line as well, and the summary lines add up to field 1002.
    summaries = [
        _assemble('SUMMARY', {1050: code, 1051: counts[code]})
        for code in _SUMMARY_ORDER if counts.get(code)
    ]
    return b''.join(_encode(line) for line in (header, *summaries))


def build_uniform_files(
    business: UniformBusiness,
    documents: Iterable[UniformDocument],
    *,
    period_start: date,
    period_end: date,
    generated_at: datetime,
    primary_id: int | None = None,
) -> UniformFiles:
    """
    INI.TXT and BKMVDATA.TXT for `documents`, written in the order given.

    Every document must be dated (1230) inside the period: the adapter selects
    and this checks, since a stray document would contradict the range the INI
    declares. `generated_at` is the export's moment (1026/1027 and the
    directory name); an aware value is read on the Israeli clock. Two exports
    in the same minute would share a directory, so the adapter moves the
    second one a minute on (2.2(ג)). `primary_id` is drawn at random when not
    given (2.4(א)).
    """
    _require_date(period_start, 'period_start')
    _require_date(period_end, 'period_end')
    if period_end < period_start:
        raise UniformFormatError(f'the period ends ({period_end}) before it starts ({period_start})')
    if business.software_type == SOFTWARE_SINGLE_YEAR and period_start.year != period_end.year:
        raise UniformFormatError('single-year software exports one tax year (1023); this period spans two')
    if not isinstance(generated_at, datetime):
        raise TypeError('generated_at: expected a datetime')
    vat = numeric_field(business.vat_number, 9, 'business VAT number')
    if int(vat) == 0:
        raise UniformFormatError('the business VAT number is missing')
    if primary_id is None:
        primary_id = new_primary_id()
    elif isinstance(primary_id, bool) or not isinstance(primary_id, int) or not 0 < primary_id < 10 ** 15:
        raise UniformFormatError(f'primary id {primary_id!r} must be a positive number of at most 15 digits')
    moment = israel_wall_clock(generated_at)
    directory = export_directory(vat, moment)

    writer = _DataWriter(vat)
    writer.write('A100', {1103: primary_id, 1104: SYSTEM_CONSTANT})
    seen: set = set()
    for link, doc in enumerate(documents, start=1):
        _check_placement(doc, business, period_start, period_end, seen)
        _write_document(writer, doc, link)
    # 1155 counts the closing record itself, so it is that record's own number.
    total = writer.next_number
    writer.write('Z900', {1153: primary_id, 1154: SYSTEM_CONSTANT, 1155: total})

    counts = MappingProxyType(dict(writer.counts))
    return UniformFiles(
        ini=_ini(business, vat, primary_id, counts, period_start, period_end, moment, directory),
        bkmvdata=b''.join(writer.records),
        counts=counts,
        primary_id=primary_id,
        directory=directory,
    )


def _zip(entries: Mapping[str, bytes], stamp: tuple[int, ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            # A fixed timestamp (the export's) keeps the archive byte-identical
            # for identical input.
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def uniform_zip(files: UniformFiles, business: UniformBusiness, generated_at: datetime) -> bytes:
    """
    The export as it is handed over: a ZIP holding OPENFRMT/<VAT8>.<YY>/<MMDDhhmm>/
    with INI.TXT and BKMVDATA.zip, the archive BKMVDATA.TXT is compressed into (2.2).

    `business` and `generated_at` must be those the files were built with: INI
    field 1012 already names the directory, and a ZIP that put the files
    anywhere else would contradict it.
    """
    directory = export_directory(business.vat_number, generated_at)
    if directory != files.directory:
        raise UniformFormatError(f'the files were built for {files.directory}, not {directory}')
    stamp = israel_wall_clock(generated_at).timetuple()[:6]
    archive = _zip({DATA_FILENAME: files.bkmvdata}, stamp)
    return _zip({f'{directory}/{INI_FILENAME}': files.ini, f'{directory}/{ARCHIVE_FILENAME}': archive}, stamp)
