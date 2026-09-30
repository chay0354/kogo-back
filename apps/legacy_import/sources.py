"""Which software a file came from, and in which format.

`source_system` names the software. It is part of every imported document's
key, so it has to be the same string every time the same software is imported:
the known ones have a fixed slug, and a name the office types is normalised
(case, spaces, quotes) before it is used.

`format` is how the file is read:

    tazman   the previous software's .xls export (parser.py) — always source 'tazman'
    table    any software's CSV / XLSX / XLS, its columns mapped by the office (columns.py)
    uniform  any software's מבנה אחיד files, BKMVDATA.TXT (+ INI.TXT) (uniform_reader.py)
"""
from __future__ import annotations

import re

from apps.legacy_import.models import SOURCE_SYSTEM_MAX_LENGTH, SOURCE_TAZMAN
from apps.legacy_import.reader import ImportFileError

FORMAT_TAZMAN = 'tazman'
FORMAT_TABLE = 'table'
FORMAT_UNIFORM = 'uniform'
FORMATS = (FORMAT_TAZMAN, FORMAT_TABLE, FORMAT_UNIFORM)

# slug -> the name the office knows it by. The order is the order the screen lists them in.
KNOWN_SOURCES = {
    SOURCE_TAZMAN: 'Tazman — התוכנה הקודמת',
    'greeninvoice': 'חשבונית ירוקה (morning)',
    'icount': 'iCount',
    'rivhit': 'רווחית',
    'ezcount': 'EZcount',
    'hashavshevet': 'חשבשבת',
    'invoice4u': 'Invoice4U',
    'sumit': 'SUMIT',
    'priority': 'Priority',
}

# Other spellings the office (or an INI.TXT's software name) may use for a known one.
_ALIASES = {
    'תזמן': SOURCE_TAZMAN,
    'חשבונית ירוקה': 'greeninvoice',
    'green invoice': 'greeninvoice',
    'morning': 'greeninvoice',
    'מורנינג': 'greeninvoice',
    'אייקאונט': 'icount',
    'רווחית': 'rivhit',
    'rivhit': 'rivhit',
    'חשבשבת': 'hashavshevet',
    'hashavshevet': 'hashavshevet',
    'סאמיט': 'sumit',
    'פריוריטי': 'priority',
}

_QUOTES = str.maketrans({'״': '"', '”': '"', '“': '"', '׳': "'", '’': "'", '‘': "'", '`': "'"})
_SPACES = re.compile(r'\s+')


def normalise_source_system(value) -> str:
    """'  Green  Invoice ' -> 'greeninvoice'; 'תוכנה של רו״ח' -> 'תוכנה של רו"ח'. '' when there is nothing."""
    text = _SPACES.sub(' ', str(value or '').translate(_QUOTES)).strip().casefold()
    text = text.replace('/', '-').replace('\\', '-')
    if not text:
        return ''
    compact = text.replace(' ', '').replace('-', '').replace('_', '').replace('.', '')
    for slug in KNOWN_SOURCES:
        if compact == slug:
            return slug
    for alias, slug in _ALIASES.items():
        if text == alias or compact == alias.replace(' ', ''):
            return slug
    return text[:SOURCE_SYSTEM_MAX_LENGTH].strip()


def require_source_system(value, *, fmt: str) -> str:
    """The source a preview is for. The previous software's own export is always Tazman's."""
    if fmt == FORMAT_TAZMAN:
        return SOURCE_TAZMAN
    slug = normalise_source_system(value)
    if len(slug) < 2:
        raise ImportFileError('יש לבחור את התוכנה שממנה הקובץ יוצא (או להקליד את שמה)')
    return slug


def source_label(slug: str) -> str:
    return KNOWN_SOURCES.get(slug, slug)


def known_sources_payload() -> list:
    return [{'id': slug, 'label': label} for slug, label in KNOWN_SOURCES.items()]
