"""
Builds the two synthetic .xls fixtures the reader tests open. Every name, number
and address in them is invented.

    export_sample.xls          — the old software's 35 columns, six documents
    export_sample_corrupt.xls  — the same workbook, with the container flaw the
                                 real export has: the root entry's short-stream
                                 container claims the Workbook stream's first
                                 sector, so xlrd says "Workbook corruption:
                                 seen[N] == 4" unless told to look past it.

xlwt is not a dependency of kogo; run this from a scratch venv that has it:

    python -m venv /tmp/xlsenv && /tmp/xlsenv/bin/pip install xlwt==1.3.0 xlrd==2.0.1
    /tmp/xlsenv/bin/python apps/legacy_import/tests/fixtures/build_fixtures.py
"""
import struct
from datetime import datetime
from pathlib import Path

import xlwt

HERE = Path(__file__).resolve().parent

HEADERS = [
    'שם פרטי', 'שם משפחה', 'אימייל', 'טלפון נייד', 'טלפון בבית', 'פקס', 'מס\' לקוח בהנה"ח חיצונית',
    'שימוש בטלפון נוסף', 'שימוש בטלפון נוסף שני', 'טלפון נוסף 1 לשליחת SMS', 'טלפון נוסף 2 לשליחת SMS',
    'תאריך לידה', 'ת"ז \\ ע"מ \\ ח"פ', 'עיר', 'מיקוד', 'כתובת', 'סיסמת כניסה לאפליקציה', 'הערות',
    'תאריך הצטרפות', 'נמחק', 'עוסק מורשה', 'סוג המסמך', 'מספר המסמך', 'פרטים', 'תאריך', 'סה"כ ח-ן',
    'סה"כ קבלה', 'סה"כ זיכוי', 'הערה', 'סטטוס', 'סוג תשלום', 'מס כרטיס (4 ספרות)', 'מיקום',
    'סכום הניכוי', 'סה"כ לפני ניכוי',
]

# (first, last, email, mobile, ext, id, type, number, details, date, invoice, receipt, credit, payment, card, location)
DOCUMENTS = [
    ('נועה', 'בדיקה', 'noa@example.test', '0500000001', 101, 12345678.0, 'חשבונית מס קבלה', 70001,
     'חוג קפוארה', datetime(2025, 1, 5, 10, 30), 236.0, 236.0, 0.0, 'כרטיס אשראי', 1234.0, 'כפר סבא'),
    ('נועה', 'בדיקה', 'noa@example.test', '0500000001', 101, 12345678.0, 'חשבונית מס קבלה', 70002,
     'חוג קפוארה', datetime(2025, 2, 5, 10, 30), 236.0, 236.0, 0.0, 'כרטיס אשראי', 1234.0, 'כפר סבא'),
    ('מתנ&#34;ס', 'הדגמה', 'office@example.test', '', 202, 512345678.0, 'חשבונית מס', 40001,
     'הדרכת קפוארה מתנס הדגמה', datetime(2025, 3, 1), 1180.0, 0.0, 0.0, '-', '-', 'סניף מתנ"ס הדגמה פ"ת'),
    ('מתנ&#34;ס', 'הדגמה', 'office@example.test', '', 202, 512345678.0, 'קבלה', 33001,
     'תשלום', datetime(2025, 3, 20), 0.0, 1180.0, 0.0, 'העברה בנקאית', '-', 'סניף מתנ"ס הדגמה פ"ת'),
    ('יוסי', 'דוגמה', '', '521111111', 303, '', 'חשבון עיסקה', 60001,
     'מופע', datetime(2025, 4, 1), 500.0, 0.0, 0.0, '-', '-', '21 הצגות מופעים ופסטיבלים במותג'),
    ('יוסי', 'דוגמה', '', '521111111', 303, '', 'חשבונית מס זיכוי', 41001,
     'זיכוי מופע', datetime(2025, 4, 2), 0.0, 0.0, 500.0, '-', '-', '21 הצגות מופעים ופסטיבלים במותג'),
]


def build(path: Path) -> None:
    book = xlwt.Workbook(encoding='utf-8')
    sheet = book.add_sheet('Worksheet')
    dated = xlwt.easyxf(num_format_str='DD/MM/YYYY HH:MM')
    for col, header in enumerate(HEADERS):
        sheet.write(0, col, header)
    for index, doc in enumerate(DOCUMENTS, start=1):
        (first, last, email, mobile, ext, id_number, doc_type, number, details, when,
         invoice, receipt, credit, payment, card, location) = doc
        values = {
            'שם פרטי': first, 'שם משפחה': last, 'אימייל': email, 'טלפון נייד': mobile,
            'טלפון בבית': '039999999', 'פקס': '039999998', 'מס\' לקוח בהנה"ח חיצונית': float(ext),
            'שימוש בטלפון נוסף': 1.0, 'תאריך לידה': '01/01/1980', 'ת"ז \\ ע"מ \\ ח"פ': id_number,
            'עיר': 'עיר בדיקה', 'כתובת': 'רחוב הדוגמה 1', 'סיסמת כניסה לאפליקציה': 'SECRET-PW-123',
            'הערות': '', 'סוג המסמך': doc_type, 'מספר המסמך': float(number), 'פרטים': details,
            'סה"כ ח-ן': invoice, 'סה"כ קבלה': receipt, 'סה"כ זיכוי': credit, 'הערה': '',
            'סטטוס': 'סגורה', 'סוג תשלום': payment, 'מס כרטיס (4 ספרות)': card, 'מיקום': location,
            'סכום הניכוי': 0.0, 'סה"כ לפני ניכוי': receipt,
        }
        for col, header in enumerate(HEADERS):
            value = values.get(header, '')
            if header in ('תאריך', 'תאריך הצטרפות'):
                sheet.write(index, col, when, dated)
            elif value != '':
                sheet.write(index, col, value)
    book.save(str(path))


def corrupt(source: Path, target: Path) -> None:
    """Point the root entry's short-stream container at the Workbook stream's first sector."""
    data = bytearray(source.read_bytes())
    sector_size = 1 << struct.unpack_from('<H', data, 30)[0]
    directory_sid = struct.unpack_from('<i', data, 48)[0]
    directory = 512 + directory_sid * sector_size
    workbook = None
    for offset in range(directory, directory + sector_size, 128):
        name_len = struct.unpack_from('<H', data, offset + 64)[0]
        name = data[offset:offset + max(name_len - 2, 0)].decode('utf-16-le')
        if name == 'Workbook':
            workbook = offset
    assert workbook is not None, 'no Workbook stream'
    first_sid = struct.unpack_from('<i', data, workbook + 116)[0]
    struct.pack_into('<i', data, directory + 116, first_sid)  # root entry's start sector
    struct.pack_into('<I', data, directory + 120, 64)  # and a non-empty size
    target.write_bytes(bytes(data))


if __name__ == '__main__':
    build(HERE / 'export_sample.xls')
    corrupt(HERE / 'export_sample.xls', HERE / 'export_sample_corrupt.xls')
    print('written')
