"""
Reading a municipality's sheet, whatever shape it arrived in.

Every municipality sends something different, so nothing here is written
against one of them. What differs is only *how much* the model is asked to do:

* **A spreadsheet has a grid, so the model never reads the people.** It is shown
  a sample and returns a description of the layout — which column is the name,
  which is the phone, which columns must never be read, and where each group's
  rows begin and end. Ordinary Python then lifts the cells. That is a few
  hundred tokens instead of a few thousand, it is seconds instead of a minute,
  and it makes inventing a child structurally impossible: every name comes from
  a cell. It also means the identity-number column is *named in order to be
  excluded* and its contents are never sent anywhere.

* **A scan has no grid, so the model does read the names.** There is no way
  around that, and no way to keep the identity numbers printed beside them from
  being sent with the page. What we can guarantee is the other half: nothing
  that comes back is stored unless it survives the scrub below.

Both paths hand back the same small shape, one municipality group at a time.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Iterable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Reading the layout is a small, structural question; reading handwriting off a
# scan is not. Effort is set per call rather than globally for that reason.
MODEL_ID = 'claude-opus-5'
LAYOUT_EFFORT = 'low'
PAGE_EFFORT = 'medium'

# How much of a sheet the model needs in order to recognise its shape. Both real
# files declare their columns in the first rows and repeat one block structure
# after that, so a sample is enough — and a sample is what keeps the identity
# column out of anything larger than one prompt.
SHEET_SAMPLE_ROWS = 45
SHEET_SAMPLE_COLS = 14

# An Israeli identity number. Anything carrying one never reaches the database:
# see scrub_identity_numbers.
ID_NUMBER = re.compile(r'\b\d{9}\b')

# A day letter and a start time, in either of the two shapes the real files use:
# 'א:16:00-16:45' (spreadsheet) and 'יום ב 16:45-17:30' (scan).
DAY_LETTERS = {'א': 0, 'ב': 1, 'ג': 2, 'ד': 3, 'ה': 4, 'ו': 5, 'ש': 6}
SLOT_RE = re.compile(r'(?:יום\s+)?([אבגדהוש])\s*:?\s*(\d{1,2}:\d{2})')


class IdentityNumberLeak(Exception):
    """Raised when text on its way to the database still carries an ID number."""


def scrub_identity_numbers(*values: str | None) -> None:
    """
    Refuse to store anything still carrying an identity number.

    The schemas below have no field for one, which handles the honest case. This
    handles the other one: a model that folds an ID into a name, or a
    spreadsheet whose name column holds both. It raises rather than strips,
    because a name that had a number cut out of it is not a name we should be
    guessing at — the group goes to the manager instead.
    """
    for value in values:
        if value and ID_NUMBER.search(str(value)):
            raise IdentityNumberLeak('נמצא מספר זהות בטקסט שמיועד לשמירה')


def parse_slots(raw: str) -> list[dict]:
    """
    Day and start time out of whatever the municipality wrote.

    Start only. Yehud lists a group as 19:00-20:00 where our lesson is
    19:00-19:45, and comparing the full range would throw away a match that is
    plainly the same class.
    """
    seen: list[dict] = []
    for letter, start in SLOT_RE.findall(raw or ''):
        hour, minute = start.split(':')
        slot = {'day': DAY_LETTERS[letter], 'start': f'{int(hour):02d}:{minute}'}
        if slot not in seen:
            seen.append(slot)
    return seen


# --------------------------------------------------------------------------
# What the model is allowed to return
# --------------------------------------------------------------------------

class SheetColumns(BaseModel):
    """Which column is what, in a spreadsheet the model has never seen before."""

    name_column: int = Field(description='0-based index of the participant name column')
    phone_column: int | None = Field(
        default=None, description='0-based index of the phone column, or null'
    )
    second_phone_column: int | None = Field(
        default=None, description='0-based index of a second phone column, or null'
    )
    excluded_columns: list[int] = Field(
        default_factory=list,
        description=(
            'Columns that must never be read or stored: identity number, date of '
            'birth, gender, customer number, and anything else identifying.'
        ),
    )


class SheetGroupBlock(BaseModel):
    """One group's block of rows inside the sheet."""

    municipality_code: str = Field(default='', description="the group's own code, if the sheet gives one")
    group_name: str = Field(default='', description='the group name as written')
    slots_raw: str = Field(default='', description='the days and times exactly as written')
    first_person_row: int = Field(description='0-based index of the first participant row')
    last_person_row: int = Field(description='0-based index of the last participant row, inclusive')
    stated_total: int | None = Field(
        default=None, description='the count the sheet states for this group, if any'
    )


class SheetLayout(BaseModel):
    """The whole answer for a spreadsheet: a map, never the data."""

    period_label: str = Field(default='', description='the period the report covers, if stated')
    stated_report_total: int | None = Field(
        default=None, description='the total the report states for itself, if any'
    )
    columns: SheetColumns
    groups: list[SheetGroupBlock]


class ScannedPerson(BaseModel):
    """One participant read off a scanned page. Name and phone, and nothing else."""

    first_name: str = Field(description='given name')
    last_name: str = Field(default='', description='family name')
    phone: str = Field(default='', description='a phone number if one is printed, else empty')


class ScannedGroup(BaseModel):
    """One scanned page: its heading, and the people actually printed on it."""

    municipality_code: str = Field(default='', description="the group's own code, if printed")
    group_name: str = Field(default='', description='the group name as printed')
    slots_raw: str = Field(default='', description='the activity days and times exactly as printed')
    stated_total: int | None = Field(
        default=None, description='the total this page states for the group, if printed'
    )
    people: list[ScannedPerson]


# --------------------------------------------------------------------------
# Talking to the model
# --------------------------------------------------------------------------

def _client():
    """
    The Anthropic client, keyed from the environment or from the settings screen.

    Same resolution as the storage key: whoever runs the hosting account is not
    always the person who needs this working.
    """
    import anthropic

    from apps.core.scoping import integration_credential

    key = integration_credential('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY is not configured')
    return anthropic.Anthropic(api_key=key)


LAYOUT_PROMPT = """את/ה קורא/ת קובץ נוכחות של מחלקת חוגים בעירייה.

הקובץ מחולק לקבוצות. לכל קבוצה יש שורת כותרת (שם הקבוצה, ולעיתים קוד קבוצה),
שורה או ציון של ימי הפעילות והשעות, ואז שורות המשתתפים.

אל תקרא/י את המשתתפים. תארי/תאר רק את **המבנה**:
- באיזה אינדקס עמודה נמצא שם המשתתף, ובאיזה הטלפון.
- אילו עמודות אסור לקרוא לעולם: תעודת זהות, תאריך לידה, מין, מספר לקוח, וכל
  עמודה מזהה אחרת. חובה לכלול את כולן ב-excluded_columns.
- לכל קבוצה: אינדקס השורה הראשונה והאחרונה של המשתתפים בפועל.

שורות ריקות שהודפסו מראש לצורך רישום ביד אינן משתתפים. שורות מפרידות
(למשל רצף של תווי שווה) אינן משתתפים. שורה שאין בה שם אינה משתתף.

האינדקסים הם 0-based ומתייחסים לשורות ולעמודות כפי שהן ממוספרות בטקסט שלמטה."""

PAGE_PROMPT = """את/ה קורא/ת עמוד מתוך דוח נוכחות מודפס של מחלקת חוגים בעירייה.

החזירי/החזר את כותרת הקבוצה ואת המשתתפים **שכתובים בפועל על העמוד**.

כללים מחייבים:
- שורות ריקות שהודפסו מראש כדי לרשום בהן ביד אינן משתתפים. אל תמציאי/תמציא אף שם.
- אל תחזירי/תחזיר תעודות זהות, תאריכי לידה או מין. שם וטלפון בלבד.
- אם מודפס מספר טלפון אחד או יותר, החזירי/החזר את הראשון בלבד.
- אם מודפס סכום משתתפים בתחתית העמוד, החזירי/החזר אותו ב-stated_total."""


def _grid_to_text(grid: list[list[str]]) -> str:
    """The sample the model sees: numbered rows and columns, tabs between cells."""
    header = '\t'.join(f'[{i}]' for i in range(min(SHEET_SAMPLE_COLS, max((len(r) for r in grid), default=0))))
    lines = [f'      \t{header}']
    for index, row in enumerate(grid[:SHEET_SAMPLE_ROWS]):
        cells = '\t'.join((c or '')[:40] for c in row[:SHEET_SAMPLE_COLS])
        lines.append(f'[{index}]\t{cells}')
    return '\n'.join(lines)


def read_sheet_layout(grid: list[list[str]]) -> tuple[SheetLayout, dict]:
    """Ask the model to describe a spreadsheet's shape. Returns (layout, usage)."""
    response = _client().messages.parse(
        model=MODEL_ID,
        max_tokens=8000,
        output_config={'effort': LAYOUT_EFFORT},
        output_format=SheetLayout,
        messages=[{
            'role': 'user',
            'content': f'{LAYOUT_PROMPT}\n\n---\n{_grid_to_text(grid)}',
        }],
    )
    usage = {
        'input_tokens': response.usage.input_tokens,
        'output_tokens': response.usage.output_tokens,
    }
    return response.parsed_output, usage


def read_scanned_page(pdf_bytes: bytes) -> tuple[ScannedGroup, dict]:
    """Ask the model to read one scanned page. Returns (group, usage)."""
    import base64

    response = _client().messages.parse(
        model=MODEL_ID,
        max_tokens=8000,
        output_config={'effort': PAGE_EFFORT},
        output_format=ScannedGroup,
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'document',
                    'source': {
                        'type': 'base64',
                        'media_type': 'application/pdf',
                        'data': base64.standard_b64encode(pdf_bytes).decode('ascii'),
                    },
                },
                {'type': 'text', 'text': PAGE_PROMPT},
            ],
        }],
    )
    usage = {
        'input_tokens': response.usage.input_tokens,
        'output_tokens': response.usage.output_tokens,
    }
    return response.parsed_output, usage


# --------------------------------------------------------------------------
# Applying a layout, deterministically
# --------------------------------------------------------------------------

def read_grid(file_bytes: bytes) -> list[list[str]]:
    """The spreadsheet as plain strings, in memory. Nothing is written here."""
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    grid: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        grid.append(['' if cell is None else str(cell).strip() for cell in row])
    workbook.close()
    return grid


def redact(grid: list[list[str]], excluded: Iterable[int]) -> list[list[str]]:
    """
    The grid with the identifying columns emptied, before anything is stored.

    This is the point the identity numbers stop existing for us. Everything
    downstream — the stored grid, the review screen, a re-read after a
    correction — works from what this returns.
    """
    drop = set(excluded)
    return [
        ['' if index in drop else cell for index, cell in enumerate(row)]
        for row in grid
    ]


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ''
    return (row[index] or '').strip()


# Surnames that are two words. Splitting on the first token alone turns
# 'בן דוד אימרי' into a child called "דוד אימרי" of family "בן", which is wrong
# in both real files — they carry בן דוד, בן יוחנה, בן טובים and בר תקוה.
COMPOUND_SURNAME_PREFIXES = {'בן', 'בר', 'אבו', 'אבן', 'דה', 'אל'}


def split_name(full: str) -> tuple[str, str]:
    """
    A written name into given and family parts.

    Both municipalities write the family name first, which is also the order a
    register is called in, so the leading token is the surname and the rest is
    the given name. A single token becomes the given name — better a first name
    with no surname than a surname nobody answers to.
    """
    parts = (full or '').split()
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    if len(parts) >= 3 and parts[0] in COMPOUND_SURNAME_PREFIXES:
        return ' '.join(parts[2:]), ' '.join(parts[:2])
    return ' '.join(parts[1:]), parts[0]


def people_from_block(
    grid: list[list[str]], columns: SheetColumns, block: SheetGroupBlock,
) -> list[dict]:
    """
    Lift one group's participants out of the redacted grid.

    A row only counts as a person if it actually holds a name, which is what
    keeps the blank rows printed for hand-writing — and the separator rows — out
    of the result even when the model's bounds are a little generous.
    """
    people: list[dict] = []
    for index in range(block.first_person_row, min(block.last_person_row + 1, len(grid))):
        row = grid[index]
        name = _cell(row, columns.name_column)
        if not name or set(name) <= {'=', '-', '_', ' '}:
            continue
        phone = _cell(row, columns.phone_column) or _cell(row, columns.second_phone_column)
        first, last = split_name(name)
        scrub_identity_numbers(first, last, phone)
        people.append({'first_name': first, 'last_name': last, 'phone': phone})
    return people


def split_pdf_pages(file_bytes: bytes) -> list[bytes]:
    """One single-page PDF per page. No rasterising — the page travels as it is."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(file_bytes))
    pages: list[bytes] = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        pages.append(buffer.getvalue())
    return pages
