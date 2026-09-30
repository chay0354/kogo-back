"""Reading another software's מבנה אחיד (uniform structure) files back into documents.

Every invoicing software registered in Israel can export its documents as
INI.TXT + BKMVDATA.TXT (הוראות להפקת קבצים במבנה אחיד, version 1.31). Kogo
writes that format (apps/documents/uniform_format.py); this module reads it
with the same record layouts — LAYOUTS there is the one transcription of the
spec's column tables, so the two can never disagree about where a field is.

What is read
------------
C100 (a document's header) makes one document. D110 (its lines) gives its
details and the document it is based on; D120 (a receipt's payments) gives how
it was paid. A000 in INI.TXT says whose books these are, which software wrote
them and in which character set. Everything else (B100/B110 ledger, M100
inventory) is not a document and is skipped.

Accepted as uploaded: BKMVDATA.TXT alone, or a ZIP holding it (and INI.TXT) at
any depth — including the software's own OPENFRMT/…/BKMVDATA.zip inside it.

Fields are cut from the bytes, then decoded: the layouts count bytes of a
single-byte code page (ISO-8859-8 / Windows-1255 when field 1029 is 1, CP-862
when it is 2). A DOS file's Hebrew is visual (reversed) — it is read as it is
and flagged, never "fixed" by guessing.
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from apps.documents.uniform_format import DOCUMENT_TYPES, LAYOUTS, PAYMENT_METHOD_CODES
from apps.legacy_import.columns import UNIFORM_TYPE_CODES, amounts_for, base_row, read_number
from apps.legacy_import.parser import TYPE_LABELS, normalise_id, normalise_phone
from apps.legacy_import.reader import ImportFileError

MAX_UNCOMPRESSED = 120_000_000
MAX_NESTING = 2
DETAILS_LINES = 5

_PAYMENT_NAMES = {
    code: name for name, code in PAYMENT_METHOD_CODES.items()
}
PAYMENT_LABELS = {
    'cash': 'מזומן', 'check': 'המחאה', 'credit_card': 'כרטיס אשראי', 'bank_transfer': 'העברה בנקאית',
    'voucher': 'תווי קניה', 'exchange_slip': 'תלוש החלפה', 'promissory_note': 'שטר',
    'standing_order': 'הוראת קבע', 'other': 'אחר',
}
_FIELDS = {code: {number: (start - 1, width, kind) for number, start, width, kind in layout}
           for code, layout in LAYOUTS.items()}
_LINE_BREAK = re.compile(rb'\r\n|\n|\r')


@dataclass
class UniformFiles:
    """What was found in the upload: the data file(s), and the INI that describes each."""
    data: list = field(default_factory=list)     # [(directory, bytes)]
    ini: dict = field(default_factory=dict)      # directory -> bytes


# --------------------------------------------------------------------------
# Finding the files
# --------------------------------------------------------------------------

def _basename(name: str) -> tuple:
    name = name.replace('\\', '/')
    directory, _, base = name.rpartition('/')
    return directory, base.upper()


def _walk_zip(content: bytes, found: UniformFiles, prefix: str, depth: int, budget: list) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ImportFileError('קובץ ה-ZIP פגום') from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            directory, base = _basename(info.filename)
            wanted = base in ('BKMVDATA.TXT', 'INI.TXT') or (base.endswith('.ZIP') and depth < MAX_NESTING)
            if not wanted:
                continue
            budget[0] -= info.file_size
            if budget[0] < 0:
                raise ImportFileError('קובץ ה-ZIP גדול מדי לקריאה. העלו כל שנה בנפרד.')
            payload = archive.read(info)
            where = f'{prefix}/{directory}'.strip('/')
            if base == 'BKMVDATA.TXT':
                found.data.append((where, payload))
            elif base == 'INI.TXT':
                found.ini[where] = payload
            else:
                # The software's own BKMVDATA.zip sits next to INI.TXT: its data belongs to that directory.
                _walk_zip(payload, found, where, depth + 1, budget)


def find_files(content: bytes) -> UniformFiles:
    found = UniformFiles()
    if content.startswith(b'PK\x03\x04'):
        _walk_zip(content, found, '', 0, [MAX_UNCOMPRESSED])
    elif content.lstrip()[:4] == b'A000':
        raise ImportFileError('זה קובץ INI.TXT בלבד. העלו את BKMVDATA.TXT, או ZIP שמכיל את שניהם.')
    elif content.lstrip()[:4] in (b'A100', b'C100'):
        found.data.append(('', content))
    else:
        raise ImportFileError('הקובץ אינו במבנה אחיד: העלו את BKMVDATA.TXT, או ZIP שמכיל אותו ואת INI.TXT.')
    if not found.data:
        raise ImportFileError('בקובץ ה-ZIP לא נמצא BKMVDATA.TXT')
    return found


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------

def _raw(record: bytes, code: str, number: int) -> bytes:
    start, width, _kind = _FIELDS[code][number]
    return record[start:start + width]


def _text(record, code, number, encoding) -> str:
    return _raw(record, code, number).decode(encoding, errors='replace').replace('�', '?').strip()


def _int(record, code, number):
    digits = _raw(record, code, number).decode('ascii', errors='ignore').strip()
    return int(digits) if digits.isdigit() else None


def _date(record, code, number):
    digits = _raw(record, code, number).decode('ascii', errors='ignore').strip()
    if len(digits) != 8 or not digits.isdigit() or not digits.strip('0'):
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def _amount(record, code, number):
    """X9(n)v99: a sign, then digits with two implied decimals. None when blank."""
    text = _raw(record, code, number).decode('ascii', errors='ignore').strip()
    if not text:
        return None
    sign = -1 if text[0] == '-' else 1
    digits = text.lstrip('+-').strip()
    if not digits.isdigit():
        return None
    return sign * (Decimal(digits) / 100).quantize(Decimal('0.01'))


# --------------------------------------------------------------------------
# INI.TXT
# --------------------------------------------------------------------------

def read_ini(content: bytes) -> dict:
    """A000's facts, and the record counts the INI declares (נספח 4)."""
    lines = [line for line in _LINE_BREAK.split(content) if line.strip()]
    header = next((line for line in lines if line[:4] == b'A000'), None)
    if header is None:
        return {}
    charset = _int(header, 'A000', 1029)
    encoding = 'cp862' if charset == 2 else 'cp1255'
    declared = {}
    for line in lines:
        if line[:4] != b'A000' and len(line) >= 19:
            code = line[:4].decode('ascii', errors='ignore')
            count = line[4:19].decode('ascii', errors='ignore').strip()
            if count.isdigit():
                declared[code] = int(count)
    return {
        'vat_number': _text(header, 'A000', 1003, encoding).lstrip('0'),
        'software': _text(header, 'A000', 1007, encoding),
        'software_version': _text(header, 'A000', 1008, encoding),
        'vendor_name': _text(header, 'A000', 1010, encoding),
        'business_name': _text(header, 'A000', 1018, encoding),
        'tax_year': _int(header, 'A000', 1023) or None,
        'period_start': _iso(_date(header, 'A000', 1024)),
        'period_end': _iso(_date(header, 'A000', 1025)),
        'produced': _iso(_date(header, 'A000', 1026)),
        'charset': charset or 1,
        'encoding': encoding,
        'declared': declared,
    }


def _iso(value):
    return value.isoformat() if value else None


# --------------------------------------------------------------------------
# BKMVDATA.TXT -> rows
# --------------------------------------------------------------------------

@dataclass
class UniformResult:
    rows: list
    skipped: list
    info: dict


def read_uniform(content: bytes, *, source_system: str) -> UniformResult:
    """The upload -> rows in the import's shape, what was skipped, and what the INI says."""
    found = find_files(content)
    headers, lines, payments, counts = [], defaultdict(list), defaultdict(list), Counter()
    inis, warnings = [], []
    for directory, data in found.data:
        ini = read_ini(found.ini[directory]) if directory in found.ini else {}
        if ini:
            inis.append(ini)
        encoding = ini.get('encoding', 'cp1255')
        own = Counter()
        for record in _LINE_BREAK.split(data):
            code = record[:4].decode('ascii', errors='ignore')
            if not record.strip():
                continue
            own[code] += 1
            if code == 'C100':
                headers.append((record, encoding))
            elif code == 'D110':
                key = (_int(record, 'D110', 1253), _text(record, 'D110', 1254, encoding))
                lines[key].append((record, encoding))
            elif code == 'D120':
                key = (_int(record, 'D120', 1303), _text(record, 'D120', 1304, encoding))
                payments[key].append(record)
        counts.update(own)
        for code, declared in ini.get('declared', {}).items():
            if code in ('C100', 'D110', 'D120') and declared != own.get(code, 0):
                warnings.append(f'ב-INI.TXT רשומות {declared} רשומות {code}, ובקובץ {own.get(code, 0)}')
        if ini.get('charset') == 2:
            warnings.append('הקובץ בקידוד DOS (CP-862): ייתכן שהטקסט בעברית יופיע הפוך')
    if not headers:
        raise ImportFileError('בקובץ אין רשומות מסמך (C100)')
    if not inis:
        warnings.append('לא נמצא INI.TXT: הקובץ נקרא בקידוד Windows-1255 (עברית)')

    rows, skipped, seen, other_types = [], [], set(), Counter()
    for position, (record, encoding) in enumerate(headers, start=1):
        row, reason = _document(record, encoding, lines, payments, source_system)
        if row is None:
            if reason.startswith('type:'):
                other_types[int(reason[5:])] += 1
            else:
                skipped.append({'row': position, 'reason': reason})
            continue
        key = (row['doc_type'], row['number'])
        if key in seen:
            skipped.append({'row': position, 'reason': 'מסמך כפול בקובץ'})
            continue
        seen.add(key)
        rows.append(row)
    for code, count in sorted(other_types.items()):
        skipped.append({'row': 0, 'reason': f'{count} מסמכים מסוג {code} ({DOCUMENT_TYPES.get(code, "לא מוכר")}) — אינם מסמכי מכירה ולא יובאו'})
    first = inis[0] if inis else {}
    info = {
        'software': first.get('software', ''),
        'software_version': first.get('software_version', ''),
        'vendor_name': first.get('vendor_name', ''),
        'business_name': first.get('business_name', ''),
        'vat_number': first.get('vat_number', ''),
        'period_start': min((i['period_start'] for i in inis if i.get('period_start')), default=None),
        'period_end': max((i['period_end'] for i in inis if i.get('period_end')), default=None),
        'files': len(found.data),
        'records': {code: counts.get(code, 0) for code in ('C100', 'D110', 'D120')},
        'warnings': warnings,
    }
    return UniformResult(rows=rows, skipped=skipped, info=info)


def _document(record, encoding, lines, payments, source_system):
    """(row, '') for one C100 — or (None, why)."""
    code = _int(record, 'C100', 1203)
    doc_type = UNIFORM_TYPE_CODES.get(str(code)) if code is not None else None
    if not doc_type:
        return None, f'type:{code or 0}'
    printed = _text(record, 'C100', 1204, encoding)
    number, original = read_number(printed)
    if number is None:
        return None, 'אין מספר מסמך'
    when = _date(record, 'C100', 1230) or _date(record, 'C100', 1205)
    if when is None:
        return None, 'אין תאריך'
    receipt = doc_type == 'receipt'
    before_vat = None if receipt else _amount(record, 'C100', 1221)
    vat = None if receipt else _amount(record, 'C100', 1222)
    total = _amount(record, 'C100', 1223)
    withholding = abs(_amount(record, 'C100', 1224) or Decimal('0.00'))
    key = (code, printed)
    own_lines = lines.get(key, [])
    details = [_text(line, 'D110', 1260, enc) for line, enc in own_lines[:DETAILS_LINES]]
    linked = next(
        (
            f"{DOCUMENT_TYPES.get(_int(line, 'D110', 1256) or 0, '')} {_text(line, 'D110', 1257, enc)}".strip()
            for line, enc in own_lines if _text(line, 'D110', 1257, enc)
        ),
        '',
    )
    methods = []
    for payment in payments.get(key, []):
        name = _PAYMENT_NAMES.get(_int(payment, 'D120', 1306) or 0)
        label = PAYMENT_LABELS.get(name, '')
        if label and label not in methods:
            methods.append(label)
    customer_vat = normalise_id(_text(record, 'C100', 1215, 'ascii'))
    customer_number = _text(record, 'C100', 1225, encoding)
    amounts = amounts_for(doc_type, before_vat, vat, total)
    amounts['withholding'] = str(withholding)
    address = ' '.join(part for part in (
        _text(record, 'C100', 1208, encoding), _text(record, 'C100', 1209, encoding),
    ) if part)
    return base_row(
        type_label=DOCUMENT_TYPES.get(code, TYPE_LABELS[doc_type])[:60],
        doc_type=doc_type,
        number=number,
        original_number=original,
        date=when.isoformat(),
        **amounts,
        status='מבוטל' if _text(record, 'C100', 1228, 'ascii') == '1' else '',
        payment_type=' + '.join(methods)[:60],
        location=_text(record, 'C100', 1231, encoding),
        details=' · '.join(d for d in details if d),
        linked_document=linked[:60],
        first_name=_text(record, 'C100', 1207, encoding),
        phone=normalise_phone(_text(record, 'C100', 1214, encoding)),
        id_number=customer_vat if customer_vat.strip('0') else '',
        ext_number=f'{source_system}:{customer_number}' if customer_number else '',
        city=_text(record, 'C100', 1210, encoding),
        address=address,
    ), ''
