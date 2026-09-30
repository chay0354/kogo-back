"""Any software's table export — CSV, XLSX or XLS — as a reader.Sheet.

Other invoicing software exports its documents as a table with a header row,
but no two agree on the file: a CSV in UTF-8 (with or without a BOM), in
Windows-1255 or in UTF-16 (Excel's "Unicode text"), separated by commas,
semicolons or tabs; an .xlsx; or an old .xls. This module only turns the file
into rows of plain values (text, float, datetime, bool, None). Which column is
what is columns.py's business, chosen by the office.

XLSX without a dependency. openpyxl is not one of kogo's requirements (it is
not in requirements.txt or uv.lock, so it is not on Vercel), and an .xlsx is
a ZIP of XML: the first worksheet, the shared strings and the date formats are
all that is needed, read with the standard library. The XML parser is expat
(no external entities; entity expansion is capped since expat 2.4.1), and every
part is size-checked before it is decompressed.
"""
from __future__ import annotations

import codecs
import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree

from apps.legacy_import.reader import ImportFileError, Sheet

XLS_MAGIC = b'\xd0\xcf\x11\xe0'
ZIP_MAGIC = b'PK\x03\x04'

# A table export of 8,000 documents is a few MB of XML; this is far above it
# and far below what would hurt a serverless function.
MAX_XML_BYTES = 60_000_000
MAX_CSV_LINES = 200_000
DELIMITERS = (',', ';', '\t', '|')

# The header row is the first of these many rows that looks like one (a title line may come first).
HEADER_SEARCH_ROWS = 20


# --------------------------------------------------------------------------
# Which file
# --------------------------------------------------------------------------

def file_kind(content: bytes, file_name: str = '') -> str:
    """'xlsx', 'xls' or 'csv' — by the bytes, not the name, which the office may have changed."""
    if content.startswith(ZIP_MAGIC):
        return 'xlsx'
    if content.startswith(XLS_MAGIC):
        return 'xls'
    return 'csv'


def read_table(content: bytes, file_name: str = '') -> Sheet:
    """The file's first table: the header row, and every row after it. Raises ImportFileError."""
    if not content:
        raise ImportFileError('הקובץ ריק')
    kind = file_kind(content, file_name)
    if kind == 'xlsx':
        headers, rows, datemode = _read_xlsx(content)
    elif kind == 'xls':
        headers, rows, datemode = _read_xls(content)
    else:
        headers, rows = _read_csv(content)
        datemode = 0
    return _with_header_row(headers, rows, datemode)


def _with_header_row(first, rest, datemode) -> Sheet:
    """
    The real header row: the first of the opening rows with at least two cells
    and at least half as many as the widest of them. A report's title line
    ("דוח מסמכים 01/2025") sits above the headers in some exports.
    """
    table = [first] + list(rest or [])
    head = table[:HEADER_SEARCH_ROWS]
    widest = max((_filled(row) for row in head), default=0)
    index = next(
        (i for i, row in enumerate(head) if _filled(row) >= 2 and _filled(row) * 2 >= widest),
        None,
    )
    if index is None:
        raise ImportFileError('לא נמצאה בקובץ שורת כותרות (שמות העמודות)')
    headers = [_header_text(cell) for cell in table[index]]
    while headers and not headers[-1]:
        headers.pop()
    width = len(headers)
    rows = [(list(row) + [None] * width)[:width] for row in table[index + 1:]]
    return Sheet(headers=headers, rows=rows, datemode=datemode)


def _filled(row) -> int:
    return sum(1 for cell in row if cell not in (None, '') and str(cell).strip())


def _header_text(cell) -> str:
    if cell is None:
        return ''
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).replace('﻿', '').strip()


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

def decode_text(content: bytes) -> str:
    """UTF-8 (BOM or not), UTF-16 with a BOM, else Windows-1255 — the Hebrew Windows code page."""
    if content.startswith(codecs.BOM_UTF8):
        return content[len(codecs.BOM_UTF8):].decode('utf-8', errors='replace')
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return content.decode('utf-16', errors='replace')
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        return content.decode('cp1255', errors='replace')


def detect_delimiter(text: str) -> str:
    """The separator: Excel's own "sep=;" line if there is one, else the one the header line splits best by."""
    first = next((line for line in text.splitlines() if line.strip()), '')
    if first.lower().startswith('sep=') and len(first.strip()) == 5:
        return first.strip()[4]
    counts = {}
    for delimiter in DELIMITERS:
        try:
            counts[delimiter] = len(next(csv.reader([first], delimiter=delimiter)))
        except (csv.Error, StopIteration):
            counts[delimiter] = 0
    best = max(DELIMITERS, key=lambda d: counts[d])
    return best if counts[best] > 1 else ','


def _read_csv(content: bytes):
    text = decode_text(content).replace('\x00', '')
    delimiter = detect_delimiter(text)
    lines = text.splitlines()
    if lines and lines[0].lower().startswith('sep='):
        lines = lines[1:]
    if len(lines) > MAX_CSV_LINES:
        raise ImportFileError('בקובץ יותר מדי שורות. פצלו אותו לכמה קבצים.')
    try:
        table = list(csv.reader(lines, delimiter=delimiter))
    except csv.Error as exc:
        raise ImportFileError('לא ניתן לקרוא את קובץ ה-CSV') from exc
    table = [[cell.strip() if isinstance(cell, str) else cell for cell in row] for row in table]
    table = [row for row in table if any(cell for cell in row)]
    if not table:
        raise ImportFileError('הקובץ ריק')
    return table[0], table[1:]


# --------------------------------------------------------------------------
# XLS (the old binary format): the same xlrd reader the previous software's export uses
# --------------------------------------------------------------------------

def _read_xls(content: bytes):
    from apps.legacy_import.reader import read_sheet

    try:
        sheet = read_sheet(content)
    except ImportFileError as exc:
        raise ImportFileError('לא ניתן לקרוא את קובץ ה-Excel (‎.xls)') from exc
    return sheet.headers, sheet.rows, sheet.datemode


# --------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------

_NS = {
    'm': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'rel': 'http://schemas.openxmlformats.org/package/2006/relationships',
}
_MAIN = '{%s}' % _NS['m']
# Excel's built-in number formats that are dates (ECMA-376 18.8.30).
_BUILTIN_DATE_FORMATS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 27, 30, 36, 45, 46, 47, 50, 57})
_DATE_CODE = re.compile(r'[dmyhs]', re.IGNORECASE)
_QUOTED = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')
_CELL_REF = re.compile(r'([A-Z]+)(\d+)')


def _part(archive: zipfile.ZipFile, name: str, required: bool = True):
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise ImportFileError('הקובץ אינו קובץ Excel תקין (‎.xlsx)')
        return None
    if info.file_size > MAX_XML_BYTES:
        raise ImportFileError('הקובץ גדול מדי לקריאה. פצלו אותו לכמה קבצים.')
    return archive.read(name)


def _first_sheet_path(archive) -> str:
    workbook = ElementTree.fromstring(_part(archive, 'xl/workbook.xml'))
    sheet = workbook.find('m:sheets/m:sheet', _NS)
    if sheet is None:
        raise ImportFileError('בקובץ אין גיליון')
    rel_id = sheet.get('{%s}id' % _NS['r'])
    rels = ElementTree.fromstring(_part(archive, 'xl/_rels/workbook.xml.rels'))
    for rel in rels.findall('rel:Relationship', _NS):
        if rel.get('Id') == rel_id:
            target = rel.get('Target', '')
            return target.lstrip('/') if target.startswith('/') else f'xl/{target}'
    raise ImportFileError('הקובץ אינו קובץ Excel תקין (‎.xlsx)')


def _date1904(archive) -> bool:
    workbook = ElementTree.fromstring(_part(archive, 'xl/workbook.xml'))
    pr = workbook.find('m:workbookPr', _NS)
    return pr is not None and pr.get('date1904') in ('1', 'true')


def _shared_strings(archive) -> list:
    raw = _part(archive, 'xl/sharedStrings.xml', required=False)
    if raw is None:
        return []
    strings = []
    for item in ElementTree.fromstring(raw).findall('m:si', _NS):
        # Plain text is one <t>; rich text is runs of <r><t>. Phonetic guides (<rPh>) are not the text.
        phonetic = {id(t) for rph in item.findall('m:rPh', _NS) for t in rph.iter(f'{_MAIN}t')}
        strings.append(''.join(t.text or '' for t in item.iter(f'{_MAIN}t') if id(t) not in phonetic))
    return strings


def _is_date_format(code: str) -> bool:
    plain = _QUOTED.sub('', code or '')
    return bool(_DATE_CODE.search(plain)) and 'General' not in plain


def _date_styles(archive) -> set:
    """The indexes of the cell styles (s="n") whose number format is a date."""
    raw = _part(archive, 'xl/styles.xml', required=False)
    if raw is None:
        return set()
    styles = ElementTree.fromstring(raw)
    custom = {
        int(fmt.get('numFmtId', '0')): fmt.get('formatCode', '')
        for fmt in styles.findall('m:numFmts/m:numFmt', _NS)
    }
    dated = set()
    for index, xf in enumerate(styles.findall('m:cellXfs/m:xf', _NS)):
        fmt = int(xf.get('numFmtId', '0') or 0)
        if fmt in _BUILTIN_DATE_FORMATS or (fmt in custom and _is_date_format(custom[fmt])):
            dated.add(index)
    return dated


def _column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def excel_date(serial: float, date1904: bool = False):
    """Excel's serial day -> datetime (1900 system, with its phantom 29/02/1900; or the 1904 system)."""
    if serial < 0:
        return None
    epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    try:
        return epoch + timedelta(days=serial)
    except OverflowError:
        return None


def _cell_value(cell, strings, dated, date1904):
    kind = cell.get('t', 'n')
    if kind == 'inlineStr':
        return ''.join(t.text or '' for t in cell.iter(f'{_MAIN}t'))
    value = cell.find('m:v', _NS)
    text = value.text if value is not None else None
    if text is None:
        return None
    if kind == 's':
        try:
            return strings[int(text)]
        except (ValueError, IndexError):
            return None
    if kind in ('str', 'e'):
        return None if kind == 'e' else text
    if kind == 'b':
        return text.strip() == '1'
    try:
        number = float(text)
    except ValueError:
        return text
    if int(cell.get('s', '0') or 0) in dated:
        return excel_date(number, date1904)
    return number


def _read_xlsx(content: bytes):
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ImportFileError('הקובץ אינו קובץ Excel תקין (‎.xlsx)') from exc
    try:
        with archive:
            path = _first_sheet_path(archive)
            strings = _shared_strings(archive)
            dated = _date_styles(archive)
            date1904 = _date1904(archive)
            sheet_xml = _part(archive, path)
            table = []
            for _event, row in ElementTree.iterparse(io.BytesIO(sheet_xml)):
                if row.tag != f'{_MAIN}row':
                    continue
                cells = {}
                for position, cell in enumerate(row.findall('m:c', _NS)):
                    match = _CELL_REF.match(cell.get('r', ''))
                    col = _column_index(match.group(1)) if match else position
                    cells[col] = _cell_value(cell, strings, dated, date1904)
                row.clear()
                width = max(cells) + 1 if cells else 0
                table.append([cells.get(i) for i in range(width)])
    except ImportFileError:
        raise
    except (ElementTree.ParseError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ImportFileError('הקובץ אינו קובץ Excel תקין (‎.xlsx)') from exc
    table = [row for row in table if any(cell not in (None, '') for cell in row)]
    if not table:
        raise ImportFileError('הגיליון בקובץ ריק')
    return table[0], table[1:], 1 if date1904 else 0
