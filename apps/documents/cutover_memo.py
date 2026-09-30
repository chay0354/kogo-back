"""
The cutover memo: the record of the move from the previous invoicing program to
Kogo, for the owner and the accountant to sign.

No rule names a form for a mid-year change of software (docs/COMPLIANCE-2026-09-18-
INVOICING.md §2.2): what is certain is that no number may repeat within a tax
year in a run (18(א)(3)), and that an issued document is never renumbered
(23(ב)). What protects the business is a signed record of where each run
stopped and where the next one started. This draws it from the database:

* when the switch happened (given by hand — the database cannot know when the
  old program was last used);
* every Kogo run (DocumentSeries): its first and last number, how many it
  handed out, the first and last document date, and, when it continues the old
  program's run, from what (DocumentSeriesOpening);
* the old program's last number of each type — from its imported documents
  (LegacyDocument) when there are any, else as typed on the command line, else
  a blank line to fill in by hand;
* the closed shared run of documents issued by hand ('2026-0042'), and the
  lesson receipts and store sales numbered from a payment's id ('INV-…'),
  which were never fiscal numbers — explained, counted, dated;
* the continuity check's gaps (numbering.continuity);
* what is still open: checks not yet due, invoices no receipt was recorded
  against, store sales on monthly billing not yet paid, drafts;
* a statement that the old program stopped issuing, and signature lines.

Read-only. The PDF is drawn with the documents' own fonts and RTL helpers
(invoice_layout.py); `--sign` signs it with the configured backend
(signing/signer.sign_pdf), which locally is the test key only.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Max, Min, Sum
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import KeepTogether, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.documents.invoice_layout import (
    BODY_TEXT,
    CARD_BORDER,
    FONT_BOLD,
    FONT_REGULAR,
    HEADING_COLOR,
    ROW_SEP,
    SUB_TEXT,
    TABLE_HEAD_BG,
    TITLE_COLOR,
    ensure_fonts_registered,
    money,
    para,
)
from apps.documents.issuer import ISSUER_ADDRESS, ISSUER_NAME, VAT_REGISTRATION_LINE

SIGN_REASON = 'מזכר מעבר בין תוכנות להפקת מסמכים — לחתימת ההנהלה ורואה החשבון'
BLANK = '______________'
MAX_OPEN_ROWS = 40
MAX_MISSING_SHOWN = 20

OLD_TYPE_LABELS = {
    'tax_invoice': 'חשבונית מס',
    'combined': 'חשבונית מס/קבלה',
    'receipt': 'קבלה',
    'transaction_invoice': 'חשבונית עסקה',
    'credit_invoice': 'חשבונית מס זיכוי',
}

_OLD_LAST = re.compile(r'^\s*([a-z_]+)\s*=\s*(\d+)\s*(?:@\s*(\d{4}-\d{2}-\d{2}))?\s*$')


class MemoInputError(ValueError):
    """A --old-last or --switch-at the memo cannot read."""


def parse_old_last(values) -> dict:
    """['combined=121882@2026-09-23', ...] -> {'combined': (121882, date(2026, 9, 23))}."""
    out = {}
    for raw in values or ():
        match = _OLD_LAST.match(str(raw))
        if not match or match.group(1) not in OLD_TYPE_LABELS:
            raise MemoInputError(
                f'--old-last {raw!r}: expected TYPE=NUMBER[@YYYY-MM-DD], TYPE one of {", ".join(OLD_TYPE_LABELS)}'
            )
        on = None
        if match.group(3):
            try:
                on = date.fromisoformat(match.group(3))
            except ValueError as exc:
                raise MemoInputError(f'--old-last {raw!r}: {match.group(3)} is not a date') from exc
        out[match.group(1)] = (int(match.group(2)), on)
    return out


def parse_switch_at(raw: str | None) -> datetime | None:
    """'2026-09-24 09:57' (Israel time) -> an aware datetime; None when not given."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).strip().replace('T', ' '))
    except ValueError as exc:
        raise MemoInputError(f'--switch-at {raw!r}: expected YYYY-MM-DD HH:MM') from exc
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


# ── the facts ───────────────────────────────────────────────────────────────

@dataclass
class RunLine:
    name: str
    label: str
    first: str
    last: str
    issued: int
    first_date: date | None
    last_date: date | None
    continues: str
    missing: tuple = ()


@dataclass
class OldLast:
    doc_type: str
    label: str
    number: int | None = None
    on: date | None = None
    source: str = ''          # 'ייבוא', 'הוזן ידנית' or ''
    old_type_name: str = ''   # the old program's own name for the type
    note: str = ''


@dataclass
class CutoverFacts:
    generated_at: datetime
    switch_at: datetime | None
    old_software: str
    runs: list = field(default_factory=list)
    old_last: list = field(default_factory=list)
    shared_runs: list = field(default_factory=list)
    old_lessons: dict = field(default_factory=dict)
    old_store: dict = field(default_factory=dict)
    pending_checks: dict = field(default_factory=dict)
    open_invoices: list = field(default_factory=list)
    open_store: dict = field(default_factory=dict)
    drafts: int = 0

    @property
    def gaps(self) -> list:
        return [run for run in self.runs if run.missing]


def _local_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


_DATE_FIELDS = {'FormalDocument': 'document_date', 'Invoice': 'invoice_date', 'StoreInvoice': 'issue_date'}


def _runs() -> list:
    from apps.documents.numbering import _series_sources, continuity

    sources = _series_sources()
    lines = []
    for run in continuity():
        if not run.series:
            continue  # the closed shared run has a section of its own
        queryset, number_field = sources[run.series]
        date_field = _DATE_FIELDS[queryset.model.__name__]
        span = queryset.filter(**{f'{number_field}__startswith': f'{run.series}-{run.year}-'}).aggregate(
            first=Min(date_field), last=Max(date_field),
        )
        lines.append(RunLine(
            name=run.name, label=run.label, first=run.first, last=run.last, issued=run.issued,
            first_date=_local_date(span['first']), last_date=_local_date(span['last']),
            continues=(f'{run.previous_type_label} · אחרון {run.previous_last_number}'
                       if run.previous_last_number is not None else ''),
            missing=run.missing,
        ))
    return lines


def _old_last(manual: dict) -> list:
    from apps.legacy_import.models import LegacyDocument

    imported = {}
    for doc_type in OLD_TYPE_LABELS:
        top = LegacyDocument.objects.filter(doc_type=doc_type).order_by('-number').first()
        if top is not None:
            imported[doc_type] = top
    out = []
    for doc_type, label in OLD_TYPE_LABELS.items():
        line = OldLast(doc_type=doc_type, label=label)
        typed = manual.get(doc_type)
        if doc_type in imported:
            top = imported[doc_type]
            line.number, line.on, line.source = top.number, top.document_date, 'ייבוא'
            line.old_type_name = top.original_type
            if typed and (typed[0] != top.number or (typed[1] and typed[1] != top.document_date)):
                on = f' מיום {typed[1]:%d/%m/%Y}' if typed[1] else ''
                line.note = f'הוזן ידנית: {typed[0]}{on} — שונה מהייבוא, לבדוק'
        elif typed:
            line.number, line.on, line.source = typed[0], typed[1], 'הוזן ידנית'
        out.append(line)
    return out


def _shared_runs() -> list:
    from apps.documents.models import DocumentCounter, FormalDocument

    out = []
    for counter in DocumentCounter.objects.order_by('year'):
        docs = FormalDocument.objects.filter(document_number__regex=rf'^{counter.year}-[0-9]{{4,}}$')
        span = docs.aggregate(first=Min('document_date'), last=Max('document_date'))
        numbers = sorted(docs.values_list('document_number', flat=True),
                         key=lambda number: int(number.split('-', 1)[1]))
        out.append({
            'year': counter.year,
            'counter': counter.counter,
            'count': len(numbers),
            'first': numbers[0] if numbers else '',
            'last': numbers[-1] if numbers else '',
            'first_date': span['first'],
            'last_date': span['last'],
        })
    return out


def _old_numbered() -> tuple[dict, dict]:
    """The lesson receipts and store sales numbered from a payment's id — never fiscal numbers."""
    from apps.customers.financial_models import Invoice
    from apps.documents.numbering import LESSON_RUN_REGEX, STORE_RUN_REGEX
    from apps.store.models import StoreInvoice

    lessons = Invoice.objects.exclude(invoice_number__regex=LESSON_RUN_REGEX)
    lesson_span = lessons.aggregate(first=Min('invoice_date'), last=Max('invoice_date'), total=Sum('amount'))
    with_ir = Invoice.objects.filter(invoice_number__regex=LESSON_RUN_REGEX, payment_id__isnull=False)
    covered = lessons.filter(payment_id__in=with_ir.values('payment_id')).count()

    store = StoreInvoice.objects.exclude(invoice_number__regex=STORE_RUN_REGEX)
    store_span = store.aggregate(first=Min('issue_date'), last=Max('issue_date'), total=Sum('total_amount'))
    return (
        {'count': lessons.count(), 'first': _local_date(lesson_span['first']),
         'last': _local_date(lesson_span['last']), 'total': lesson_span['total'] or Decimal('0'),
         'covered_by_ir': covered},
        {'count': store.count(), 'first': _local_date(store_span['first']),
         'last': _local_date(store_span['last']), 'total': store_span['total'] or Decimal('0'),
         'with_tranzila_copy': store.filter(formal_document__isnull=False).count()},
    )


def _pending_checks() -> dict:
    from apps.documents.models import CheckItem

    items = CheckItem.objects.filter(status='pending', plan__status='active')
    span = items.aggregate(first=Min('due_date'), last=Max('due_date'), total=Sum('amount'))
    return {'count': items.count(), 'first': span['first'], 'last': span['last'],
            'total': span['total'] or Decimal('0')}


def _open_invoices() -> list:
    """
    Tax invoices and transaction invoices still owing, by the one rule the
    collections tab uses (apps/documents/settlement.py): a settlement, a
    receipt that named the invoice's number, the check or cash plan's receipt
    that paid it, and the credit notes against it. What is left is what the
    records say — the accountant confirms it.
    """
    from apps.documents.models import FormalDocument
    from apps.documents.settlement import balances

    invoices = list(
        FormalDocument.objects
        .filter(document_type__in=('tax_invoice', 'transaction_invoice'))
        .select_related('child', 'business_customer')
        .order_by('document_date', 'document_number')
    )
    owed = balances(invoices)
    out = []
    for doc in invoices:
        remaining = owed[doc.pk].open
        if remaining <= 0:
            continue
        if doc.business_customer_id and doc.business_customer:
            customer = doc.business_customer.full_name
        elif doc.child_id and doc.child:
            customer = doc.child.full_name
        else:
            customer = (doc.customer_name or '').strip()
        out.append({'number': doc.document_number, 'date': doc.document_date, 'customer': customer,
                    'type': doc.get_document_type_display(), 'total': doc.total_amount, 'open': remaining})
    return out


def _open_store() -> dict:
    from apps.store.models import StoreInvoice

    sales = StoreInvoice.objects.filter(invoice_number__startswith='SD-').exclude(
        payment_status__in=('completed', 'refunded', 'failed'),
    )
    return {'count': sales.count(), 'total': sales.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')}


def gather_facts(*, switch_at: datetime | None = None, old_software: str = '',
                 manual_old_last: dict | None = None, now: datetime | None = None) -> CutoverFacts:
    from apps.documents.models import FormalDocument

    old_lessons, old_store = _old_numbered()
    return CutoverFacts(
        generated_at=now or timezone.now(),
        switch_at=switch_at,
        old_software=(old_software or '').strip(),
        runs=_runs(),
        old_last=_old_last(manual_old_last or {}),
        shared_runs=_shared_runs(),
        old_lessons=old_lessons,
        old_store=old_store,
        pending_checks=_pending_checks(),
        open_invoices=_open_invoices(),
        open_store=_open_store(),
        drafts=FormalDocument.objects.filter(document_type='draft').count(),
    )


# ── the PDF ─────────────────────────────────────────────────────────────────

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 40
WIDTH = PAGE_WIDTH - 2 * MARGIN
# reportlab's frame and table cells each keep 6pt on either side. para() wraps
# to the width it is given, so it is given what is really there — otherwise
# Paragraph wraps a second time and moves the wrong word (invoice_layout.para).
PADDING = 12


def _styles() -> dict:
    return {
        'title': ParagraphStyle('MemoTitle', fontName=FONT_BOLD, fontSize=16, leading=22,
                                textColor=TITLE_COLOR, alignment=TA_CENTER),
        'sub': ParagraphStyle('MemoSub', fontName=FONT_REGULAR, fontSize=9, leading=13,
                              textColor=SUB_TEXT, alignment=TA_CENTER),
        'h': ParagraphStyle('MemoHeading', fontName=FONT_BOLD, fontSize=11.5, leading=16,
                            textColor=HEADING_COLOR, alignment=TA_RIGHT),
        'body': ParagraphStyle('MemoBody', fontName=FONT_REGULAR, fontSize=9, leading=13.5,
                               textColor=BODY_TEXT, alignment=TA_RIGHT),
        'th': ParagraphStyle('MemoTh', fontName=FONT_BOLD, fontSize=8, leading=10.5,
                             textColor=colors.white, alignment=TA_CENTER),
        'td': ParagraphStyle('MemoTd', fontName=FONT_REGULAR, fontSize=8, leading=11,
                             textColor=BODY_TEXT, alignment=TA_CENTER),
        'td_right': ParagraphStyle('MemoTdRight', fontName=FONT_REGULAR, fontSize=8, leading=11,
                                   textColor=BODY_TEXT, alignment=TA_RIGHT),
    }


def _day(value) -> str:
    return value.strftime('%d/%m/%Y') if value else ''


def _table(headers: list, rows: list, widths: list, styles: dict, right_columns=(0,)) -> Table:
    """A table read right to left: the first logical column is drawn on the right."""
    head = [para(text, styles['th'], width - PADDING) for text, width in zip(headers, widths)]
    body = [
        [para(str(cell), styles['td_right' if index in right_columns else 'td'], width - PADDING)
         for index, (cell, width) in enumerate(zip(row, widths))]
        for row in rows
    ]
    data = [list(reversed(line)) for line in [head] + body]
    table = Table(data, colWidths=list(reversed(widths)), repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), TABLE_HEAD_BG),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEBELOW', (0, 1), (-1, -1), 0.5, ROW_SEP),
        ('BOX', (0, 0), (-1, -1), 0.75, CARD_BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    return table


def _p(text: str, styles: dict, style: str = 'body'):
    return para(text, styles[style], WIDTH - PADDING)


def build_story(facts: CutoverFacts) -> list:
    styles = _styles()
    story = [
        _p('מזכר מעבר בין תוכנות להפקת מסמכים', styles, 'title'),
        _p(f'{ISSUER_NAME} · {VAT_REGISTRATION_LINE} · {ISSUER_ADDRESS}', styles, 'sub'),
        _p(f'הופק מנתוני Kogo ביום {timezone.localtime(facts.generated_at):%d/%m/%Y בשעה %H:%M}. '
           'הנתונים נכונים למועד ההפקה.', styles, 'sub'),
        Spacer(1, 12),
    ]

    # 1. The switch.
    switch = timezone.localtime(facts.switch_at) if facts.switch_at else None
    story += [
        _p('1. פרטי המעבר', styles, 'h'),
        _p(f'מועד המעבר ל־Kogo: {f"{switch:%d/%m/%Y} בשעה {switch:%H:%M}" if switch else BLANK} (שעון ישראל).',
           styles),
        _p(f'התוכנה הקודמת: {facts.old_software or BLANK}.', styles),
        _p('מאותו מועד כל מסמכי המכירה והקבלות מופקים ב־Kogo בלבד, בסדרות המפורטות בסעיף 2. '
           'מסמך שהונפק לא ממוספר מחדש ולא משתנה; תיקון נעשה במסמך נוסף בלבד (הוראות ניהול פנקסי '
           'חשבונות, סעיף 23(ב)).', styles),
        Spacer(1, 8),
    ]

    # 2. Kogo's runs.
    story.append(_p('2. סדרות המספור ב־Kogo', styles, 'h'))
    if facts.runs:
        widths = [92, 84, 84, 34, 62, 62, WIDTH - 418]
        rows = [
            [f'{run.name}\n{run.label}', run.first, run.last, run.issued,
             _day(run.first_date), _day(run.last_date), run.continues or 'מתחילה ב־1']
            for run in facts.runs
        ]
        story.append(_table(
            ['סדרה', 'מספר ראשון', 'מספר אחרון', 'כמות', 'תאריך ראשון', 'תאריך אחרון', 'המשך לתוכנה הקודמת'],
            rows, widths, styles,
        ))
    else:
        story.append(_p('טרם הונפקו מסמכים בסדרות של Kogo.', styles))
    story += [
        _p('כל סדרה רצה בתוך שנת מס ואינה חוזרת על מספר (סעיף 18(א)(3)). סדרה שממשיכה את הסדרה של '
           'התוכנה הקודמת נפתחת מהמספר שאחרי האחרון שם, ונרשמה במערכת עם מי פתח אותה ומתי.', styles),
        Spacer(1, 8),
    ]

    # 3. The old program's last numbers.
    story.append(_p('3. המספרים האחרונים בתוכנה הקודמת', styles, 'h'))
    rows = [
        [line.label + (f' ({line.old_type_name})' if line.old_type_name and line.old_type_name != line.label else ''),
         line.number if line.number is not None else BLANK,
         _day(line.on) if line.on else BLANK,
         line.source or 'להשלמה ידנית',
         line.note]
        for line in facts.old_last
    ]
    story.append(_table(['סוג המסמך', 'מספר אחרון', 'תאריך', 'מקור', 'הערה'], rows,
                        [130, 80, 70, 80, WIDTH - 360], styles, right_columns=(0, 4)))
    story += [Spacer(1, 8)]

    # 4. The closed shared run.
    story.append(_p('4. הסדרה המשותפת הסגורה של מסמכים ידניים', styles, 'h'))
    story.append(_p('לפני שכל סוג מסמך קיבל סדרה משלו (11/09/2026), המסמכים הידניים ב־Kogo מוספרו '
                    'בסדרה אחת לכל השנה, בפורמט YYYY-NNNN. הסדרה נסגרה: לא נלקחים ממנה מספרים, '
                    'והיא עדיין נבדקת ברצף.', styles))
    if facts.shared_runs:
        story.append(_table(
            ['שנה', 'המונה', 'מסמכים', 'מספר ראשון', 'מספר אחרון', 'תאריך ראשון', 'תאריך אחרון'],
            [[run['year'], run['counter'], run['count'], run['first'], run['last'],
              _day(run['first_date']), _day(run['last_date'])] for run in facts.shared_runs],
            [50, 55, 55, 85, 85, 90, WIDTH - 420], styles, right_columns=(),
        ))
    else:
        story.append(_p('הסדרה לא נפתחה.', styles))
    story += [Spacer(1, 8)]

    # 5. The numbers that were never fiscal.
    lessons, store = facts.old_lessons, facts.old_store
    story += [
        _p('5. קבלות ומכירות במספור הישן (INV-…) — אינן מספרים פיסקליים', styles, 'h'),
        _p('עד שנפתחו הסדרות IR, ST ו־SD, חיובי החוגים ומכירות החנות קיבלו מספר שנגזר ממזהה התשלום. '
           'המספר ייחודי אך אינו עוקב, ולכן אינו מספר של מסמך במערכת החשבונות: בדוחות Kogo הם '
           'מופיעים כ"הכנסה ללא מסמך".', styles),
        _p(f'חיובי חוגים: {lessons["count"]} רשומות, מ־{_day(lessons["first"])} עד {_day(lessons["last"])}, '
           f'סה"כ {money(lessons["total"])}. מהן {lessons["covered_by_ir"]} שלאותו חיוב הופקה גם קבלה בסדרת IR.'
           if lessons['count'] else 'חיובי חוגים: אין.', styles),
        _p(f'מכירות חנות: {store["count"]} רשומות, מ־{_day(store["first"])} עד {_day(store["last"])}, '
           f'סה"כ {money(store["total"])}. מהן {store["with_tranzila_copy"]} שהמסמך שלהן הופק בטרנזילה.'
           if store['count'] else 'מכירות חנות: אין.', styles),
        Spacer(1, 8),
    ]

    # 6. Continuity.
    story.append(_p('6. בדיקת רצף המספרים (נספח ה\'(א)(5))', styles, 'h'))
    if not facts.runs:
        story.append(_p('אין סדרות לבדוק.', styles))
    elif not facts.gaps:
        story.append(_p('כל מספר שכל סדרה הקצתה נמצא על מסמך קיים — אין חוסרים.', styles))
    for run in facts.gaps:
        shown = ', '.join(run.missing[:MAX_MISSING_SHOWN])
        more = f' ועוד {len(run.missing) - MAX_MISSING_SHOWN}' if len(run.missing) > MAX_MISSING_SHOWN else ''
        story.append(_p(f'{run.name}: חסרים {len(run.missing)} — {shown}{more}', styles))
    story += [Spacer(1, 8)]

    # 7. Open items.
    checks = facts.pending_checks
    story += [
        _p('7. פריטים פתוחים במועד ההפקה', styles, 'h'),
        _p(f"צ'קים דחויים שטרם נפרעו: {checks['count']}, סה\"כ {money(checks['total'])}"
           + (f", לפירעון מ־{_day(checks['first'])} עד {_day(checks['last'])}" if checks['count'] else '') + '.',
           styles),
        _p(f'מכירות חנות בחשבונית עסקה שטרם שולמו: {facts.open_store["count"]}, '
           f'סה"כ {money(facts.open_store["total"])}.', styles),
        _p(f'טיוטות שטרם אושרו: {facts.drafts}.', styles),
        _p(f'חשבוניות שלא נרשמה מולן קבלה או קיזוז במערכת: {len(facts.open_invoices)}'
           + (f', יתרה פתוחה {money(sum((row["open"] for row in facts.open_invoices), Decimal("0")))}'
              if facts.open_invoices else '') + '.', styles),
    ]
    if facts.open_invoices:
        rows = [[row['number'], _day(row['date']), row['customer'], row['type'], money(row['total']),
                 money(row['open'])] for row in facts.open_invoices[:MAX_OPEN_ROWS]]
        story.append(_table(['מספר', 'תאריך', 'לקוח', 'סוג', 'סה"כ', 'יתרה'], rows,
                            [90, 60, WIDTH - 380, 90, 70, 70], styles, right_columns=(2, 3)))
        if len(facts.open_invoices) > MAX_OPEN_ROWS:
            story.append(_p(f'ועוד {len(facts.open_invoices) - MAX_OPEN_ROWS} חשבוניות — ברשימה המלאה במערכת.',
                            styles))
    story += [Spacer(1, 10)]

    # 8. The statement and the signatures.
    old_name = facts.old_software or BLANK
    stopped = f'{switch:%d/%m/%Y} בשעה {switch:%H:%M}' if switch else f'{BLANK} בשעה ________'
    signatures = _table(
        ['', 'שם', 'תאריך', 'חתימה'],
        [['בעל העסק / מנהל', '', '', ''], ['רואה החשבון', '', '', '']],
        [120, 150, 90, WIDTH - 360], styles, right_columns=(0,),
    )
    signatures.setStyle(TableStyle([('BOTTOMPADDING', (0, 1), (-1, -1), 18),
                                    ('TOPPADDING', (0, 1), (-1, -1), 18)]))
    story.append(KeepTogether([
        _p('8. הצהרה וחתימות', styles, 'h'),
        _p(f'אנו מאשרים כי התוכנה הקודמת ({old_name}) הפסיקה להפיק מסמכים ביום {stopped}, וכי מאותו מועד '
           'לא הופק בה אף מסמך. המספרים האחרונים שהונפקו בה הם המפורטים בסעיף 3, והסדרות של Kogo הן '
           'המפורטות בסעיף 2.', styles),
        Spacer(1, 10),
        signatures,
    ]))
    return story


def _footer(canvas, document):
    canvas.saveState()
    canvas.setFont(FONT_REGULAR, 7.5)
    canvas.setFillColor(SUB_TEXT)
    canvas.drawCentredString(PAGE_WIDTH / 2, 22, f'{document.page}')
    canvas.restoreState()


def render_memo_pdf(facts: CutoverFacts) -> bytes:
    ensure_fonts_registered()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title='מזכר מעבר בין תוכנות', author=ISSUER_NAME,
    )
    document.build(build_story(facts), onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def cutover_memo_pdf(*, switch_at=None, old_software='', manual_old_last=None, sign=False, now=None) -> bytes:
    """The memo's PDF — signed with the configured backend when `sign` (SigningUnavailable if it cannot be)."""
    pdf = render_memo_pdf(gather_facts(switch_at=switch_at, old_software=old_software,
                                       manual_old_last=manual_old_last, now=now))
    if sign:
        from apps.documents.signing.signer import sign_pdf

        pdf = sign_pdf(pdf, reason=SIGN_REASON)
    return pdf

