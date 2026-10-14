"""
A small .xlsx writer — enough for a report the accountant opens in Excel.

A CSV opens left to right, in narrow columns, with nothing bold and no way to
tell a total from a row; the owner asked for a file that reads well in Excel
(14.10.2026). An .xlsx is a zip of a few XML parts, so this writes them
directly rather than adding a dependency: sheets read right to left, a frozen
and filterable header row, column widths, sums as numbers in shekel format,
dates as real dates, and a handful of row looks (header, subtotal, total,
muted, credit).

Only what the reports use. Strings are inline (no shared-string table), which
every Excel since 2007 reads.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

# Row looks: each is one cellXfs entry in styles.xml, text and number variants.
PLAIN, HEADER, TITLE, NOTE, SUBTOTAL, TOTAL, MUTED, CREDIT, SECTION = (
    'plain', 'header', 'title', 'note', 'subtotal', 'total', 'muted', 'credit', 'section',
)

# (font, fill) per look; the style index of a cell is chosen by look and kind.
_LOOKS = {
    PLAIN: (0, 0),
    HEADER: (1, 2),
    TITLE: (2, 0),
    NOTE: (3, 0),
    SUBTOTAL: (4, 3),
    TOTAL: (4, 4),
    MUTED: (3, 0),
    CREDIT: (5, 0),
    SECTION: (4, 5),
}
_KINDS = ('text', 'money', 'date', 'count')
_NUM_FMTS = {'text': 0, 'money': 164, 'date': 165, 'count': 1}

_INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')
_BAD_SHEET_CHARS = re.compile(r'[\[\]:*?/\\]')


def _style_index(look: str, kind: str) -> int:
    return 1 + list(_LOOKS).index(look) * len(_KINDS) + _KINDS.index(kind)


@dataclass
class Money:
    """A sum to print in shekel format (a Decimal is one too; this marks a 0 that must show)."""
    value: Decimal


@dataclass
class Sheet:
    title: str
    widths: list
    rows: list = field(default_factory=list)
    # The row (1-based) that holds the column headers: frozen, and filterable.
    header_row: int = 0
    # Whether the header row carries Excel's filter buttons (a banded sheet with
    # subtotal rows reads better frozen but unfiltered).
    filterable: bool = True
    # A summary reads as a page, without the grid; a table keeps it.
    gridlines: bool = True
    merges: list = field(default_factory=list)

    def add(self, values, look: str = PLAIN, *, header: bool = False, merge_to: int = 0) -> int:
        """Append a row; returns its 1-based number. merge_to merges A..that column (a title or a band)."""
        self.rows.append((list(values), look))
        number = len(self.rows)
        if header:
            self.header_row = number
        if merge_to:
            self.merges.append(f'A{number}:{_column(merge_to)}{number}')
        return number

    def blank(self) -> None:
        self.rows.append(([], PLAIN))


def _column(index: int) -> str:
    """1 → A, 27 → AA."""
    name = ''
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell(ref: str, value, look: str) -> str:
    if value is None or value == '':
        return f'<c r="{ref}" s="{_style_index(look, "text")}"/>'
    if isinstance(value, Money):
        value = value.value
    if isinstance(value, bool):
        value = 'כן' if value else 'לא'
    if isinstance(value, (Decimal, float)):
        return f'<c r="{ref}" s="{_style_index(look, "money")}"><v>{Decimal(str(value)):.2f}</v></c>'
    if isinstance(value, int):
        return f'<c r="{ref}" s="{_style_index(look, "count")}"><v>{value}</v></c>'
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        serial = (value - date(1899, 12, 30)).days
        return f'<c r="{ref}" s="{_style_index(look, "date")}"><v>{serial}</v></c>'
    text = escape(_INVALID_XML.sub('', str(value)))
    return f'<c r="{ref}" s="{_style_index(look, "text")}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        f'<sheetViews><sheetView rightToLeft="1"{"" if sheet.gridlines else " showGridLines=\"0\""} workbookViewId="0">',
    ]
    if sheet.header_row:
        top = f'A{sheet.header_row + 1}'
        out.append(f'<pane ySplit="{sheet.header_row}" topLeftCell="{top}" activePane="bottomLeft" state="frozen"/>'
                   f'<selection pane="bottomLeft" activeCell="{top}" sqref="{top}"/>')
    out.append('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="18"/><cols>')
    for index, width in enumerate(sheet.widths, start=1):
        out.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>')
    out.append('</cols><sheetData>')
    for number, (values, look) in enumerate(sheet.rows, start=1):
        height = ' ht="24" customHeight="1"' if look == TITLE else ''
        cells = ''.join(_cell(f'{_column(i)}{number}', v, look) for i, v in enumerate(values, start=1))
        out.append(f'<row r="{number}"{height}>{cells}</row>')
    out.append('</sheetData>')
    if sheet.header_row and sheet.filterable:
        out.append(f'<autoFilter ref="{_filter_range(sheet)}"/>')
    if sheet.merges:
        out.append(f'<mergeCells count="{len(sheet.merges)}">'
                   + ''.join(f'<mergeCell ref="{ref}"/>' for ref in sheet.merges) + '</mergeCells>')
    out.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
               '<pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/>'
               '</worksheet>')
    return ''.join(out)


def _filter_range(sheet: Sheet) -> str:
    """The header row down to the last row of the block under it (the first blank row ends it)."""
    last = sheet.header_row
    for number in range(sheet.header_row + 1, len(sheet.rows) + 1):
        if not sheet.rows[number - 1][0]:
            break
        last = number
    return f'A{sheet.header_row}:{_column(len(sheet.widths))}{last}'


def _absolute(ref: str) -> str:
    """A1:L88 → $A$1:$L$88."""
    return re.sub(r'([A-Z]+)(\d+)', r'$\1$\2', ref)


def _styles_xml() -> str:
    fonts = [
        '<font><sz val="11"/><name val="Arial"/></font>',
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font>',
        '<font><b/><sz val="15"/><color rgb="FF2E2A6E"/><name val="Arial"/></font>',
        '<font><sz val="10"/><color rgb="FF6B6B80"/><name val="Arial"/></font>',
        '<font><b/><sz val="11"/><color rgb="FF2E2A6E"/><name val="Arial"/></font>',
        '<font><sz val="11"/><color rgb="FFC0392B"/><name val="Arial"/></font>',
    ]
    fills = [
        '<fill><patternFill patternType="none"/></fill>',
        '<fill><patternFill patternType="gray125"/></fill>',
        '<fill><patternFill patternType="solid"><fgColor rgb="FF3B3593"/><bgColor indexed="64"/></patternFill></fill>',
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF1EFFA"/><bgColor indexed="64"/></patternFill></fill>',
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE3DFF6"/><bgColor indexed="64"/></patternFill></fill>',
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF7F6FB"/><bgColor indexed="64"/></patternFill></fill>',
    ]
    xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
    for look, (font, fill) in _LOOKS.items():
        for kind in _KINDS:
            border = 1 if look in (SUBTOTAL, TOTAL) else 0
            # Text and sums keep Excel's own alignment, which a right-to-left
            # sheet already mirrors; headers, dates and counts are centred.
            horizontal = ' horizontal="center"' if look == HEADER or kind in ('date', 'count') else ''
            wrap = ' wrapText="1"' if look in (HEADER, NOTE) else ''
            xfs.append(
                f'<xf numFmtId="{_NUM_FMTS[kind]}" fontId="{font}" fillId="{fill}" borderId="{border}" xfId="0" '
                f'applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
                f'<alignment{horizontal} vertical="center"{wrap} readingOrder="2"/></xf>'
            )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2"><numFmt numFmtId="164" formatCode="&quot;₪&quot;#,##0.00;-&quot;₪&quot;#,##0.00"/>'
        '<numFmt numFmtId="165" formatCode="dd/mm/yyyy"/></numFmts>'
        f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
        f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left/><right/><top style="thin"><color rgb="FF3B3593"/></top><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _sheet_name(title: str, used: set) -> str:
    name = _BAD_SHEET_CHARS.sub(' ', title).strip()[:31] or 'גיליון'
    base, n = name, 2
    while name in used:
        suffix = f' {n}'
        name, n = base[:31 - len(suffix)] + suffix, n + 1
    used.add(name)
    return name


def workbook(sheets: list) -> bytes:
    """The .xlsx file of these sheets, in order; the first is the one Excel opens on."""
    used: set = set()
    names = [_sheet_name(sheet.title, used) for sheet in sheets]
    defined = ''.join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
        f"'{escape(name)}'!{_absolute(_filter_range(sheet))}</definedName>"
        for i, (sheet, name) in enumerate(zip(sheets, names)) if sheet.header_row and sheet.filterable
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView activeTab="0"/></bookViews><sheets>'
        + ''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, 1))
        + '</sheets>' + (f'<definedNames>{defined}</definedNames>' if defined else '') + '</workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + ''.join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        + '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', root_rels)
        archive.writestr('xl/workbook.xml', workbook_xml)
        archive.writestr('xl/_rels/workbook.xml.rels', rels)
        archive.writestr('xl/styles.xml', _styles_xml())
        for i, sheet in enumerate(sheets, 1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml', _sheet_xml(sheet))
    return buffer.getvalue()


XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
