"""Opening the previous software's export — a BIFF .xls written by PhpSpreadsheet.

The file is a real Excel 97 workbook, but its compound-document container is
slightly wrong: one sector is claimed by two streams. xlrd refuses it
("Workbook corruption: seen[2] == 4") unless told to look past it, and Excel
itself opens it without complaint, so the flag is not hiding damage the owner
could see. The workbook stream it then reads is whole — every row and cell of
the sheet comes back.

This module is the only place that knows about xlrd. It hands back plain
Python values (text, float, datetime, None), so everything after it is tested
without a spreadsheet, and a different reader (a CSV, an .xlsx) is one function
away. It does not choose columns: parser.py picks the ones it keeps by header,
and the rest never leave this function's frame.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field


class ImportFileError(ValueError):
    """The file cannot be read as the export. The message is for the owner, in Hebrew."""


@dataclass
class Sheet:
    headers: list
    rows: list = field(default_factory=list)  # each a list of cell values, as long as headers
    # 0 for the 1900 date system (what PhpSpreadsheet writes), 1 for 1904: how
    # a date stored as a plain number is turned back into a date.
    datemode: int = 0


def read_sheet(content: bytes, keep=None) -> Sheet:
    """
    The first worksheet: its header row, and every row after it.

    `keep(headers) -> set of column indexes`: only those cells are read; every
    other one comes back as None. The export carries columns kogo must never
    hold (the customer's app password, a birth date), and a cell that is never
    read cannot be stored, logged or returned by mistake further down.
    """
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - a deployment without the dependency
        raise ImportFileError('רכיב קריאת קובצי Excel אינו מותקן בשרת') from exc

    try:
        # logfile: xlrd prints the corrupt sector map to stdout, and the server
        # log is no place for a dump of somebody's file.
        book = xlrd.open_workbook(
            file_contents=content,
            ignore_workbook_corruption=True,
            logfile=io.StringIO(),
            on_demand=True,
        )
    except xlrd.biffh.XLRDError as exc:
        # xlrd 2 reads only .xls; an .xlsx arrives here as "Excel xlsx file; not supported".
        raise ImportFileError('הקובץ אינו קובץ Excel מסוג ‎.xls שהתוכנה הקודמת מייצאת') from exc
    except Exception as exc:  # noqa: BLE001 - a broken file raises anything from struct to IndexError
        raise ImportFileError('לא ניתן לקרוא את הקובץ. ודאו שזה קובץ הייצוא ‎.xls מהתוכנה הקודמת') from exc

    try:
        if book.nsheets < 1:
            raise ImportFileError('בקובץ אין גיליון')
        sheet = book.sheet_by_index(0)
        if sheet.nrows < 1:
            raise ImportFileError('הגיליון בקובץ ריק')
        headers = [str(_cell_value(cell, book.datemode) or '') for cell in sheet.row(0)]
        wanted = set(range(len(headers))) if keep is None else set(keep(headers))
        rows = []
        for index in range(1, sheet.nrows):
            cells = sheet.row(index)
            rows.append([
                _cell_value(cells[col], book.datemode) if col in wanted and col < len(cells) else None
                for col in range(len(headers))
            ])
        return Sheet(headers=headers, rows=rows, datemode=book.datemode)
    finally:
        book.release_resources()


def _cell_value(cell, datemode):
    import xlrd

    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
            return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    return cell.value
