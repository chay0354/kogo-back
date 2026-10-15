"""Render the period invoice report as a branded, multi-page Hebrew PDF.

Built on apps.store.invoice_pdf, which is where this project's Hebrew-in-PDF
answer already lives: Heebo embedded as a TTF, and python-bidi reordering text
into visual order because reportlab draws glyphs in the order given and does
not implement the bidi algorithm itself. Fonts, brand colours and the money
formatter are imported from there rather than restated, so the report matches
the documents it summarises.

Two things this report needs that a one-page invoice never did:

* Long customer names wrap. Reordering a whole string once and letting
  Paragraph wrap the already-reordered text breaks the line in the wrong place
  and scrambles the name, so text is measured and wrapped first and each
  resulting line is reordered on its own — the technique the rental agreement
  generator documents. `_rtl_cell` does that here.
* The table runs past one page. `repeatRows=1` carries the column headers onto
  every page, and NumberedCanvas does a second pass so each page can print
  "page N of M" alongside the period and grouping.
"""
from __future__ import annotations

import io
import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as canvas_module
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from apps.documents.period_report import (
    GROUP_BY_CATEGORY,
    GROUP_BY_UNIT,
    DOCUMENT_TYPE_LABELS,
    TYPE_ORDER,
    GROUP_BY_BRANCH,
    PeriodReport,
    ReportGroup,
)
from apps.store.invoice_pdf import (
    BORDER,
    BRAND_NAVY,
    BRAND_ORANGE,
    BRAND_PURPLE,
    PANEL_BG,
    _ensure_fonts_registered,
    _rtl,
)

PAGE_WIDTH, PAGE_HEIGHT = A4
# The smallest a cell may shrink to so that a long number stays on one line.
MIN_FONT_SIZE = 6.0


def _money(amount) -> str:
    """
    ₪41,353.00 — thousands separated, and the minus before the shekel sign.

    The store's invoice format (invoice_pdf._money) prints ₪41353.00 and, for a
    negative subtotal, ₪-72.03 beside a row that reads -₪72.03. A forty-page
    report is read by its totals, so they get the separators an accountant's
    eye expects and one way of writing a minus.
    """
    value = Decimal(str(amount))
    return f'{"-" if value < 0 else ""}₪{abs(value):,.2f}'

# A 20-page table cannot afford the 5.2cm letterhead the single-page invoices
# use, so the report keeps the logo and the brand palette but reclaims the rest
# of the band for rows.
SIDE_MARGIN = 1.5 * cm
TOP_MARGIN = 2.9 * cm
BOTTOM_MARGIN = 1.9 * cm

_LOGO_IMAGE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'store', 'assets', 'letterhead', 'image2.png'
)

GROUP_BY_TITLES = {
    GROUP_BY_BRANCH: 'לפי סניפים',
    'business': 'לפי לקוחות עסקיים',
    GROUP_BY_UNIT: 'לפי עסק',
    GROUP_BY_CATEGORY: 'לפי קטגוריה',
}

# Column widths sum to the printable width (A4 minus both margins): 18cm.
# They summed to 19 until 14.10.2026, and every table ran 1cm into the left margin.
COL_WIDTHS = [
    4.3 * cm,   # customer
    3.1 * cm,   # document number — IR-2026-000123 on one line
    2.8 * cm,   # document type — חשבונית מס/קבלה on one line
    1.9 * cm,   # date
    2.0 * cm,   # net (before VAT)
    1.7 * cm,   # VAT
    2.2 * cm,   # total
]

COLUMN_HEADERS = [
    'שם לקוח', 'מספר מסמך', 'סוג', 'תאריך', 'לפני מע"מ', 'מע"מ', 'סה"כ',
]


def _styles() -> dict:
    return {
        'title': ParagraphStyle(
            'RepTitle', fontName='Heebo-Bold', fontSize=19,
            textColor=BRAND_PURPLE, alignment=TA_CENTER, leading=23,
        ),
        'subtitle': ParagraphStyle(
            'RepSubtitle', fontName='Heebo', fontSize=11,
            textColor=BRAND_NAVY, alignment=TA_CENTER, leading=15,
        ),
        'group': ParagraphStyle(
            'RepGroup', fontName='Heebo-Bold', fontSize=12,
            textColor=colors.white, alignment=TA_RIGHT, leading=16,
        ),
        'th': ParagraphStyle(
            'RepTh', fontName='Heebo-Bold', fontSize=8.5,
            textColor=colors.white, alignment=TA_CENTER, leading=11,
        ),
        'td': ParagraphStyle(
            'RepTd', fontName='Heebo', fontSize=8.5,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=11,
        ),
        'td_num': ParagraphStyle(
            'RepTdNum', fontName='Heebo', fontSize=8.5,
            textColor=BRAND_NAVY, alignment=TA_CENTER, leading=11,
        ),
        'td_credit': ParagraphStyle(
            'RepTdCredit', fontName='Heebo', fontSize=8.5,
            textColor=BRAND_ORANGE, alignment=TA_CENTER, leading=11,
        ),
        'sub': ParagraphStyle(
            'RepSub', fontName='Heebo-Bold', fontSize=8.5,
            textColor=BRAND_NAVY, alignment=TA_CENTER, leading=11,
        ),
        # A section band names the document type its rows belong to; the
        # subsection line is that type's own subtotal, quieter than the group's.
        'section': ParagraphStyle(
            'RepSection', fontName='Heebo-Bold', fontSize=9,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=12,
        ),
        'subsection': ParagraphStyle(
            'RepSubSection', fontName='Heebo-Bold', fontSize=8.5,
            textColor=BRAND_PURPLE, alignment=TA_CENTER, leading=11,
        ),
        'label': ParagraphStyle(
            'RepLabel', fontName='Heebo-Bold', fontSize=10,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=14,
        ),
        'value': ParagraphStyle(
            'RepValue', fontName='Heebo', fontSize=10,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=14,
        ),
        'grand': ParagraphStyle(
            'RepGrand', fontName='Heebo-Bold', fontSize=13,
            textColor=BRAND_PURPLE, alignment=TA_RIGHT, leading=17,
        ),
        'note': ParagraphStyle(
            'RepNote', fontName='Heebo', fontSize=8.5,
            textColor=colors.HexColor('#6b7280'), alignment=TA_RIGHT, leading=12,
        ),
        'empty': ParagraphStyle(
            'RepEmpty', fontName='Heebo-Bold', fontSize=13,
            textColor=BRAND_NAVY, alignment=TA_CENTER, leading=20,
        ),
    }


def _rtl_cell(text: str, style: ParagraphStyle, width: float, pad: float = 5) -> Paragraph:
    """
    Wrap to `width` first, then bidi-reorder each line on its own.

    Reordering the whole string and letting Paragraph wrap it afterwards would
    wrap visually-ordered text at logical word boundaries and scramble a long
    Hebrew name. `pad` is the cell's padding on each side: the lines must fit
    the width the Paragraph really gets, or it wraps them a second time and
    the first word of a line drops to the next ("…זיכויים) / סה"כ"). Tables
    here pad by 4–5pt, the totals panels by 10, a note outside a table by 0.
    """
    text = str(text or '')
    usable = max(width - 2 * pad - 2, 10)
    words = text.split(' ')
    # A number, a reference or a sum is one word, and broken over two lines it
    # reads as two (IR-2026-00000 / 2). Such a word shrinks the cell's font
    # until it fits instead; Hebrew words still wrap between themselves.
    widest = max((pdfmetrics.stringWidth(word, style.fontName, style.fontSize) for word in words), default=0)
    if widest > usable:
        size = max(style.fontSize * usable / widest, MIN_FONT_SIZE)
        style = ParagraphStyle(f'{style.name}-fit', parent=style, fontSize=size,
                               leading=min(style.leading, size * 1.25))
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = ' '.join(current + [word])
        if not current or pdfmetrics.stringWidth(candidate, style.fontName, style.fontSize) <= usable:
            current.append(word)
        else:
            lines.append(' '.join(current))
            current = [word]
    if current:
        lines.append(' '.join(current))
    return Paragraph('<br/>'.join(_rtl(line) for line in lines), style)


def _note(text: str, styles: dict) -> Paragraph:
    """A note under a table, wrapped line by line like a cell so its words stay in order."""
    # A paragraph outside a table sits in the page frame, which pads it by 6pt a side.
    return _rtl_cell(text, styles['note'], PAGE_WIDTH - 2 * SIDE_MARGIN, pad=7)


def _signed_money(amount: Decimal, is_credit: bool) -> str:
    """Credits are stored positive; the minus is display only."""
    return f'-{_money(amount)}' if is_credit else _money(amount)


class NumberedCanvas(canvas_module.Canvas):
    """
    Defers every page so the running header can print the true page count.

    A single-pass canvas cannot know the total while drawing page 1, and the
    owner asked for a report that stays readable on page 14 — which means each
    page has to say which page it is and of how many.
    """

    def __init__(self, *args, **kwargs):
        self._report_context = kwargs.pop('report_context', {})
        super().__init__(*args, **kwargs)
        self._saved_states = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_furniture(total)
            super().showPage()
        super().save()

    def _draw_furniture(self, total_pages: int) -> None:
        ctx = self._report_context
        self.saveState()

        if os.path.isfile(_LOGO_IMAGE):
            logo_w = 2.6 * cm
            logo_h = logo_w * (524 / 656)
            self.drawImage(
                _LOGO_IMAGE,
                SIDE_MARGIN,
                PAGE_HEIGHT - logo_h - 0.5 * cm,
                width=logo_w, height=logo_h,
                preserveAspectRatio=True, mask='auto',
            )

        # Running header: what this report is, on every page, so page 14 still
        # identifies itself without the reader paging back to page 1.
        self.setFont('Heebo-Bold', 9)
        self.setFillColor(BRAND_PURPLE)
        self.drawRightString(
            PAGE_WIDTH - SIDE_MARGIN, PAGE_HEIGHT - 1.15 * cm,
            _rtl(ctx.get('header', '')),
        )
        self.setFont('Heebo', 8)
        self.setFillColor(BRAND_NAVY)
        self.drawRightString(
            PAGE_WIDTH - SIDE_MARGIN, PAGE_HEIGHT - 1.75 * cm,
            _rtl(ctx.get('subheader', '')),
        )

        self.setStrokeColor(BORDER)
        self.setLineWidth(0.7)
        self.line(
            SIDE_MARGIN, PAGE_HEIGHT - 2.15 * cm,
            PAGE_WIDTH - SIDE_MARGIN, PAGE_HEIGHT - 2.15 * cm,
        )
        self.line(
            SIDE_MARGIN, BOTTOM_MARGIN - 0.5 * cm,
            PAGE_WIDTH - SIDE_MARGIN, BOTTOM_MARGIN - 0.5 * cm,
        )

        self.setFont('Heebo', 8)
        self.setFillColor(BRAND_NAVY)
        self.drawRightString(
            PAGE_WIDTH - SIDE_MARGIN, BOTTOM_MARGIN - 1.05 * cm,
            _rtl(f'עמוד {self.getPageNumber()} מתוך {total_pages}'),
        )
        self.drawString(
            SIDE_MARGIN, BOTTOM_MARGIN - 1.05 * cm,
            _rtl(ctx.get('footer', 'קוגומלו')),
        )
        self.restoreState()


CARD_RADIUS = 7
# Grey enough to read as "listed, not counted" without vanishing on print.
MUTED = colors.HexColor('#8a8aa3')
SECTION_BG = colors.HexColor('#f1effa')
SUBTOTAL_BG = colors.HexColor('#e8e5f7')
CREDIT_BG = colors.HexColor('#fdeee8')


def _card(extra=None, padding=(4, 4, 4, 4)) -> TableStyle:
    """
    The base look every table shares: a rounded card with hairlines between
    rows and no vertical rules. A grid on a rounded shape leaves lines poking
    out of the corners, so separation is by row only.
    """
    top, bottom, left, right = padding
    style = [
        ('ROUNDEDCORNERS', [CARD_RADIUS] * 4),
        ('LINEBELOW', (0, 0), (-1, -2), 0.35, BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), top),
        ('BOTTOMPADDING', (0, 0), (-1, -1), bottom),
        ('LEFTPADDING', (0, 0), (-1, -1), left),
        ('RIGHTPADDING', (0, 0), (-1, -1), right),
    ]
    return TableStyle(style + list(extra or []))


def _group_header(group: ReportGroup, styles: dict) -> Table:
    count_label = f'{group.totals.count} מסמכים' if group.totals.count != 1 else 'מסמך אחד'
    bar = Table(
        [[
            _rtl_cell(group.title, styles['group'], 11.0 * cm),
            _rtl_cell(count_label, ParagraphStyle(
                'GrpCount', parent=styles['group'], fontSize=9.5, alignment=TA_CENTER,
            ), 4.0 * cm),
        ]],
        colWidths=[12.0 * cm, sum(COL_WIDTHS) - 12.0 * cm],
        hAlign='RIGHT',
    )
    bar.setStyle(_card([('BACKGROUND', (0, 0), (-1, -1), BRAND_NAVY)], padding=(7, 7, 8, 8)))
    return bar


def _summary_row(label: str, totals, styles: dict, style_key: str = 'sub') -> list:
    # The label spans the first three columns (the table SPANs them), so a
    # long branch name stays on one line beside its totals.
    st = styles[style_key]
    return [
        _rtl_cell(label, st, sum(COL_WIDTHS[:3])),
        '',
        '',
        _rtl_cell(f'{totals.count} שורות', st, COL_WIDTHS[3]),
        _rtl_cell(_money(totals.net_amount), st, COL_WIDTHS[4]),
        _rtl_cell(_money(totals.vat_amount), st, COL_WIDTHS[5]),
        _rtl_cell(_money(totals.total_amount), st, COL_WIDTHS[6]),
    ]


def _group_table(group: ReportGroup, styles: dict) -> Table:
    """
    A group's rows, split into one section per document type.

    Each section carries its own subtotal so a tax invoice and the receipt that
    paid it are never silently summed as if they were two sales; the group
    total below them is the plain column sum, credits carrying their sign.
    """
    header = [
        _rtl_cell(text, styles['th'], width)
        for text, width in zip(COLUMN_HEADERS, COL_WIDTHS)
    ]
    data = [header]
    band_rows, credit_rows, subtotal_rows = [], [], []

    for section in group.sections:
        band_rows.append(len(data))
        data.append([
            _rtl_cell(section.label, styles['section'], sum(COL_WIDTHS)),
            *['' for _ in COL_WIDTHS[1:]],
        ])
        for row in section.rows:
            if row.is_credit:
                credit_rows.append(len(data))
            amount_style = styles['td_credit'] if row.is_credit else styles['td_num']
            data.append([
                _rtl_cell(row.customer, styles['td'], COL_WIDTHS[0]),
                _rtl_cell(row.document_number, styles['td_num'], COL_WIDTHS[1]),
                _rtl_cell(row.document_type_label, styles['td_num'], COL_WIDTHS[2]),
                _rtl_cell(row.document_date.strftime('%d/%m/%Y'), styles['td_num'], COL_WIDTHS[3]),
                _rtl_cell(_signed_money(row.net_amount, row.is_credit), amount_style, COL_WIDTHS[4]),
                _rtl_cell(_signed_money(row.vat_amount, row.is_credit), amount_style, COL_WIDTHS[5]),
                _rtl_cell(_signed_money(row.total_amount, row.is_credit), amount_style, COL_WIDTHS[6]),
            ])
        if len(group.sections) > 1:
            subtotal_rows.append(len(data))
            data.append(_summary_row(f'סה"כ {section.label}', section.totals, styles, 'subsection'))

    data.append(_summary_row(f'סיכום — {group.title}', group.totals, styles))

    # repeatRows=1 is what carries the column headers onto pages 2..N.
    table = Table(data, colWidths=COL_WIDTHS, hAlign='RIGHT', repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, PANEL_BG]),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ]
    style.append(('SPAN', (0, -1), (2, -1)))
    for index in band_rows:
        style.append(('SPAN', (0, index), (-1, index)))
        style.append(('BACKGROUND', (0, index), (-1, index), SECTION_BG))
        # A section's title never ends a page alone: it moves with its first row.
        style.append(('NOSPLIT', (0, index), (-1, min(index + 1, len(data) - 1))))
    for index in subtotal_rows:
        style.append(('SPAN', (0, index), (2, index)))
        style.append(('BACKGROUND', (0, index), (-1, index), colors.HexColor('#f6f4fc')))
        style.append(('LINEABOVE', (0, index), (-1, index), 0.6, BRAND_PURPLE))
    for index in credit_rows:
        style.append(('BACKGROUND', (0, index), (-1, index), CREDIT_BG))
    table.setStyle(_card(style))
    return table


def _totals_panel(report: PeriodReport, styles: dict):
    totals = report.totals
    rows = [
        ['סה"כ שורות בדוח', str(totals.count)],
        ['סה"כ לפני מע"מ', _money(totals.net_amount)],
        ['סה"כ מע"מ', _money(totals.vat_amount)],
        ['סה"כ כולל מע"מ', _money(totals.total_amount)],
    ]
    if totals.credits_total > 0:
        rows.append(['מזה חיובים', _money(totals.charges_total)])
        rows.append(['מזה זיכויים', f'-{_money(totals.credits_total)}'])
    # Three figures rather than one: a receipt that settles an invoice is not
    # a second sale, and a transaction invoice is not a tax document.
    rows.append(['הכנסות — חשבוניות מס ומס/קבלה, בניכוי זיכויים', _money(report.revenue_total)])
    rows.append(['תקבולים — קבלות וחשבוניות מס/קבלה', _money(report.collected_total)])
    if report.non_fiscal_total:
        rows.append(['חשבונות עסקה (אינם מסמכי מס)', _money(report.non_fiscal_total)])

    data = [
        [_rtl_cell(label, styles['label'], 8.0 * cm, pad=10),
         _rtl_cell(value, styles['value'], 4.5 * cm, pad=10)]
        for label, value in rows
    ]
    net_label = 'סה"כ נטו לתקופה (חיובים בניכוי זיכויים)' if totals.credits_total > 0 else 'סה"כ לתקופה'
    data.append([
        _rtl_cell(net_label, ParagraphStyle('GrandL', parent=styles['grand']), 8.0 * cm, pad=10),
        _rtl_cell(_money(totals.net_of_credits), styles['grand'], 4.5 * cm, pad=10),
    ])

    table = Table(data, colWidths=[8.0 * cm, 4.5 * cm], hAlign='RIGHT')
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, -2), PANEL_BG),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(6, 6, 10, 10)))
    return table


def _overview_panel(report: PeriodReport, styles: dict) -> list:
    """
    The period's money in five lines, on the first page.

    The same figures close the report, after forty pages of rows; the owner
    asked for the important thing first (14.10.2026). Nothing here is computed
    afresh — each line is a total the sections below print in full.
    """
    from apps.documents.register import money_lines, period_take

    data = [
        [_rtl_cell(label, styles['label'], 8.0 * cm, pad=10), _rtl_cell(_money(amount), styles['value'], 4.5 * cm, pad=10)]
        for label, amount in money_lines(report) if amount or label.startswith('תקבולים')
    ]
    grand = period_take(report)
    data.append([
        _rtl_cell('סה"כ כסף שנכנס בתקופה', ParagraphStyle('OverviewL', parent=styles['grand']), 8.0 * cm, pad=10),
        _rtl_cell(_money(grand), styles['grand'], 4.5 * cm, pad=10),
    ])
    table = Table(data, colWidths=[8.0 * cm, 4.5 * cm], hAlign='RIGHT')
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, -2), PANEL_BG),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(6, 6, 10, 10)))
    counts = [f'{report.totals.count} מסמכים']
    if report.undocumented is not None and report.undocumented.count:
        counts.append(f'{report.undocumented.count} חיובים ללא מסמך')
    if report.void_rows:
        counts.append(f'{len(report.void_rows)} מספרים שלא הפכו למסמך')
    return [KeepTogether([
        Paragraph(_rtl('תמונת מצב'), styles['label']),
        Spacer(1, 0.15 * cm),
        table,
        Spacer(1, 0.1 * cm),
        _note(' · '.join(counts) + ' — הפירוט המלא בהמשך.', styles),
    ]), Spacer(1, 0.6 * cm)]


def _type_breakdown(report: PeriodReport, styles: dict):
    """
    Totals per document type.

    A tax invoice and the receipt that settles it are two documents for one sum
    of money, so a single grand total can overstate the period. Splitting the
    total by type puts that in front of the reader instead of burying it.
    """
    if not report.type_totals:
        return []
    header = [
        _rtl_cell('סוג מסמך', styles['th'], 6.0 * cm),
        _rtl_cell('כמות', styles['th'], 2.0 * cm),
        _rtl_cell('לפני מע"מ', styles['th'], 2.5 * cm),
        _rtl_cell('מע"מ', styles['th'], 2.2 * cm),
        _rtl_cell('סה"כ', styles['th'], 2.5 * cm),
    ]
    data = [header]
    rank = {code: index for index, code in enumerate(TYPE_ORDER)}
    for code in sorted(report.type_totals, key=lambda c: rank.get(c, len(rank))):
        bucket = report.type_totals[code]
        label = DOCUMENT_TYPE_LABELS.get(code, code)
        data.append([
            _rtl_cell(label, styles['td'], 6.0 * cm),
            _rtl_cell(str(bucket.count), styles['td_num'], 2.0 * cm),
            _rtl_cell(_money(bucket.net_amount), styles['td_num'], 2.5 * cm),
            _rtl_cell(_money(bucket.vat_amount), styles['td_num'], 2.2 * cm),
            _rtl_cell(_money(bucket.total_amount), styles['td_num'], 2.5 * cm),
        ])
    widths = [6.0 * cm, 2.0 * cm, 2.5 * cm, 2.2 * cm, 2.5 * cm]
    table = Table(data, colWidths=widths, hAlign='RIGHT', repeatRows=1)
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
    ], padding=(5, 5, 5, 5)))
    return [
        Paragraph(_rtl('פילוח לפי סוג מסמך'), styles['label']),
        Spacer(1, 0.15 * cm),
        table,
    ]


def _group_totals_summary(report: PeriodReport, styles: dict):
    """Per-group totals in one place, so the final page answers 'how much per branch'."""
    if not report.groups:
        return []
    group_word = {GROUP_BY_BRANCH: 'סניף', GROUP_BY_UNIT: 'עסק', GROUP_BY_CATEGORY: 'קטגוריה'}.get(report.group_by, 'לקוח עסקי')
    widths = [7.0 * cm, 2.0 * cm, 2.6 * cm, 2.2 * cm, 2.6 * cm]
    header = [
        _rtl_cell(group_word, styles['th'], widths[0]),
        _rtl_cell('כמות', styles['th'], widths[1]),
        _rtl_cell('לפני מע"מ', styles['th'], widths[2]),
        _rtl_cell('מע"מ', styles['th'], widths[3]),
        _rtl_cell('סה"כ', styles['th'], widths[4]),
    ]
    data = [header]
    for group in report.groups:
        data.append([
            _rtl_cell(group.title, styles['td'], widths[0]),
            _rtl_cell(str(group.totals.count), styles['td_num'], widths[1]),
            _rtl_cell(_money(group.totals.net_amount), styles['td_num'], widths[2]),
            _rtl_cell(_money(group.totals.vat_amount), styles['td_num'], widths[3]),
            _rtl_cell(_money(group.totals.total_amount), styles['td_num'], widths[4]),
        ])
    data.append([
        _rtl_cell('סה"כ', styles['sub'], widths[0]),
        _rtl_cell(str(report.totals.count), styles['sub'], widths[1]),
        _rtl_cell(_money(report.totals.net_amount), styles['sub'], widths[2]),
        _rtl_cell(_money(report.totals.vat_amount), styles['sub'], widths[3]),
        _rtl_cell(_money(report.totals.total_amount), styles['sub'], widths[4]),
    ])
    table = Table(data, colWidths=widths, hAlign='RIGHT', repeatRows=1)
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, PANEL_BG]),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(5, 5, 5, 5)))
    return [
        Paragraph(_rtl(f'סיכום לפי {group_word}'), styles['label']),
        Spacer(1, 0.15 * cm),
        table,
    ]


def _reconciliation_block(report: PeriodReport, styles) -> list:
    """
    האם מה שבדוח מסתדר מול טרנזילה.

    הבדיקה נעשית דרך ה-API של טרנזילה עם מפתחות המסוף שכבר מוגדרים בשרת.
    כשטרנזילה לא ענתה — נאמר כאן שלא נבדק, ולא מוצג מספר מקומי כאילו הוא
    מאומת. זה ההבדל בין דוח שאפשר לסמוך עליו לבין דוח שנראה כאילו אפשר.
    """
    recon = report.reconciliation
    if not recon:
        return []

    out = [
        Spacer(1, 0.7 * cm),
        Paragraph(_rtl('אימות מול טרנזילה'), styles['label']),
        Spacer(1, 0.15 * cm),
    ]

    if not recon.get('reachable'):
        reason = recon.get('error') or 'לא התקבלה תשובה'
        out.append(_note(f'לא בוצע אימות: טרנזילה לא הייתה זמינה בעת הפקת הדוח ({reason}). '
                 f'המספרים בדוח הם של המערכת בלבד.', styles))
        return out

    tz = recon.get('tranzila') or {}
    local = recon.get('local') or {}
    only_tz = recon.get('only_in_tranzila') or []
    only_crm = recon.get('only_in_crm') or []
    unmatchable = recon.get('unmatchable') or []

    out.append(Paragraph(
        _rtl(f'טרנזילה מדווחת {tz.get("count", 0)} מסמכים בסך '
             f'{_money(Decimal(str(tz.get("total", 0))))} · '
             f'במערכת {local.get("count", 0)} מסמכים בסך '
             f'{_money(Decimal(str(local.get("total", 0))))}.'),
        styles['subtitle'],
    ))

    if not tz.get('count') and not local.get('count') and not report.is_empty:
        # The period's documents were issued here, not in Tranzila's documents
        # module, so neither side has anything to hold against the other. Said
        # in one line: the 0 beside a list of twenty "missing" documents read
        # to the accountant as a gap (September 2026).
        return out[:-1] + [_note('המסמכים בתקופה הופקו במערכת ולא במודול המסמכים של טרנזילה, '
                 'ולכן אין מסמכים להשוות מולה.', styles)]

    if recon.get('agrees'):
        # Two empty sides agree, and saying only that would read as an all-clear
        # on a period that took money in and issued nothing for it.
        if not tz.get('count') and not local.get('count') and report.undocumented_total:
            out.append(_note(f'לא הופק ולו מסמך אחד בתקופה — לא במערכת ולא בטרנזילה — '
                     f'בעוד שנכנסו {_money(report.undocumented_total)}. '
                     f'שני הצדדים ריקים ולכן הם מסכימים, אך אין כאן אישור לדבר.', styles))
            return out
        out.append(_note('כל מסמך שטרנזילה מכירה נמצא גם במערכת, ולהפך. אין פער.', styles))
        return out

    def _listing(title, rows):
        listed = ', '.join(
            str(row.get('document_number') or row.get('tranzila_doc_id') or '—')
            for row in rows[:12]
        )
        more = f' (ועוד {len(rows) - 12})' if len(rows) > 12 else ''
        return _note(f'{title}: {listed}{more}', styles)

    if only_tz:
        out.append(_listing(f'קיימים בטרנזילה ולא במערכת ({len(only_tz)})', only_tz))
    if only_crm:
        out.append(_listing(f'קיימים במערכת ולא בטרנזילה ({len(only_crm)})', only_crm))
    if unmatchable:
        out.append(_listing(
            f'ללא מספר מסמך או מזהה טרנזילה, ולכן לא ניתן להשוות ({len(unmatchable)})',
            unmatchable,
        ))
    return out


# Six columns: the fifth names the branch when the report is grouped by
# something else, and the kind of charge when the branch is already the header.
UNDOC_WIDTHS = [4.0 * cm, 4.6 * cm, 2.1 * cm, 2.5 * cm, 2.5 * cm, 2.3 * cm]

SHORT_SOURCE_LABELS = {
    'lessons': 'חוג',
    'store': 'חנות',
    'rentals': 'שכירות',
    'orphan_charges': 'חיוב ללא חשבונית',
    'payment_links': 'קישור',
    'late_receipts': 'קבלה באיחור',
}

GROUP_WORD = {GROUP_BY_BRANCH: 'סניף', GROUP_BY_UNIT: 'עסק', GROUP_BY_CATEGORY: 'קטגוריה'}


def _undocumented_group_header(group, styles: dict) -> Table:
    count_label = f'{group.count} חיובים' if group.count != 1 else 'חיוב אחד'
    bar = Table(
        [[
            _rtl_cell(group.title, styles['group'], 11.0 * cm),
            _rtl_cell(count_label, ParagraphStyle(
                'UndocCount', parent=styles['group'], fontSize=9.5, alignment=TA_CENTER,
            ), 4.0 * cm),
        ]],
        colWidths=[12.0 * cm, sum(UNDOC_WIDTHS) - 12.0 * cm],
        hAlign='RIGHT',
    )
    bar.setStyle(_card([('BACKGROUND', (0, 0), (-1, -1), BRAND_NAVY)], padding=(7, 7, 8, 8)))
    return bar


def _undocumented_group_table(group, group_by: str, styles: dict) -> Table:
    """
    One group's charges, banded by source, ending in the group's subtotal —
    the same shape as a group of documents, so the two halves of the report
    read alike. A merged row stays on the page in grey with its amount in
    brackets, so the reader can check the match by eye; it is left out of
    every subtotal.
    """
    fifth_header = 'סוג' if group_by == GROUP_BY_BRANCH else 'סניף'
    headers = ['שם לקוח', 'אסמכתה', 'תאריך', 'פירוט', fifth_header, 'סכום']
    data = [[_rtl_cell(h, styles['th'], w) for h, w in zip(headers, UNDOC_WIDTHS)]]
    band_rows, subtotal_rows, merged_rows = [], [], []

    sections = group.sections
    for section in sections:
        band_rows.append(len(data))
        data.append([
            _rtl_cell(section.label, styles['section'], sum(UNDOC_WIDTHS)),
            *['' for _ in UNDOC_WIDTHS[1:]],
        ])
        for row in section.rows:
            if row.merged_document:
                merged_rows.append(len(data))
                detail = f'מוזג עם מסמך {row.merged_document}'
                amount = f'({_money(row.amount)})'
            else:
                detail = row.detail
                amount = _money(row.amount)
            if group_by == GROUP_BY_BRANCH:
                fifth = SHORT_SOURCE_LABELS.get(row.source, '')
            else:
                # A delivery has no branch by design; "unassigned" under a
                # header that files it under the brand would read as an error.
                fifth = 'משלוח' if row.is_delivery else row.branch_name
            data.append([
                _rtl_cell(row.customer, styles['td'], UNDOC_WIDTHS[0]),
                _rtl_cell(row.reference, styles['td'], UNDOC_WIDTHS[1]),
                _rtl_cell(row.row_date.strftime('%d/%m/%Y'), styles['td'], UNDOC_WIDTHS[2]),
                _rtl_cell(detail, styles['td'], UNDOC_WIDTHS[3]),
                _rtl_cell(fifth, styles['td'], UNDOC_WIDTHS[4]),
                _rtl_cell(amount, styles['td_num'], UNDOC_WIDTHS[5]),
            ])
        if len(sections) > 1:
            subtotal_rows.append(len(data))
            data.append([
                _rtl_cell(f'סה"כ {section.label} ({section.count})', styles['subsection'],
                          sum(UNDOC_WIDTHS[:5])),
                '', '', '', '',
                _rtl_cell(_money(section.total), styles['subsection'], UNDOC_WIDTHS[5]),
            ])

    data.append([
        _rtl_cell(f'סיכום — {group.title} ({group.count} חיובים)', styles['sub'], sum(UNDOC_WIDTHS[:5])),
        '', '', '', '',
        _rtl_cell(_money(group.total), styles['sub'], UNDOC_WIDTHS[5]),
    ])

    table = Table(data, colWidths=UNDOC_WIDTHS, hAlign='RIGHT', repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, PANEL_BG]),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
        ('SPAN', (0, -1), (4, -1)),
    ]
    for index in band_rows:
        style.append(('SPAN', (0, index), (-1, index)))
        style.append(('BACKGROUND', (0, index), (-1, index), SECTION_BG))
        style.append(('NOSPLIT', (0, index), (-1, min(index + 1, len(data) - 1))))
    for index in subtotal_rows:
        style.append(('SPAN', (0, index), (4, index)))
        style.append(('BACKGROUND', (0, index), (-1, index), colors.HexColor('#f6f4fc')))
        style.append(('LINEABOVE', (0, index), (-1, index), 0.6, BRAND_PURPLE))
    for index in merged_rows:
        style.append(('TEXTCOLOR', (0, index), (-1, index), MUTED))
    table.setStyle(_card(style, padding=(5, 5, 5, 5)))
    return table


REFUND_WIDTHS = [2.1 * cm, 3.4 * cm, 4.5 * cm, 2.2 * cm, 3.6 * cm, 2.2 * cm]


def _uncredited_refunds_block(undoc, styles: dict) -> list:
    """
    Card refunds no credit note covers, one row each, and their total.

    They come off the period's take in the panel below, so each one is named
    here with what it refunded and Tranzila's own number for it — the reader
    can find it in the terminal's report and decide what document it still owes.
    """
    refunds = getattr(undoc, 'refunds', None) or []
    if not refunds:
        return []
    headers = ['תאריך', 'שם לקוח', 'המסמך שזוכה', 'מספר בטרנזילה', 'סניף', 'סכום']
    data = [[_rtl_cell(h, styles['th'], w) for h, w in zip(headers, REFUND_WIDTHS)]]
    for row in refunds:
        data.append([
            _rtl_cell(row.row_date.strftime('%d/%m/%Y'), styles['td_num'], REFUND_WIDTHS[0]),
            _rtl_cell(row.customer, styles['td'], REFUND_WIDTHS[1]),
            _rtl_cell(row.reference or '—', styles['td_num'], REFUND_WIDTHS[2]),
            _rtl_cell(row.detail or '—', styles['td_num'], REFUND_WIDTHS[3]),
            _rtl_cell(row.branch_name, styles['td'], REFUND_WIDTHS[4]),
            _rtl_cell(_money(-row.amount), styles['td_credit'], REFUND_WIDTHS[5]),
        ])
    data.append([
        _rtl_cell(f'סה"כ זיכויים ללא חשבונית זיכוי ({len(refunds)})', styles['sub'], sum(REFUND_WIDTHS[:5])),
        '', '', '', '',
        _rtl_cell(_money(-undoc.refunds_total), styles['sub'], REFUND_WIDTHS[5]),
    ])
    table = Table(data, colWidths=REFUND_WIDTHS, hAlign='RIGHT', repeatRows=1)
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, CREDIT_BG]),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
        ('SPAN', (0, -1), (4, -1)),
    ], padding=(5, 5, 5, 5)))
    return [
        KeepTogether([
            Paragraph(_rtl('זיכויים בכרטיס שלא הופקה להם חשבונית זיכוי'), styles['label']),
            Spacer(1, 0.15 * cm),
            _note('כסף שהוחזר ללקוחות בתקופה, ושום חשבונית זיכוי אינה מקושרת למסמך שזוכה. '
                  'הוא יורד מסה"כ הכסף שנכנס. רק זיכויים שבוצעו דרך המערכת — זיכוי שבוצע '
                  'במסך של טרנזילה עצמו אינו רשום כאן.', styles),
            Spacer(1, 0.15 * cm),
        ]),
        table,
        Spacer(1, 0.5 * cm),
    ]


def _undocumented_block(report: PeriodReport, styles: dict) -> list:
    """
    תקבולים בתקופה שלא הופק להם מסמך פורמלי, מקובצים כמו המסמכים.

    הסעיף הזה מופרד מהמסמכים ולא מחובר לסכומיהם. חיוב חוגים שולח להורה PDF
    שהמערכת מייצרת בעצמה — הוא אינו מסמך שהופק בטרנזילה ואין לו מספר מהמונה,
    ולכן אי אפשר להציג אותו בשורה אחת עם חשבונית. להשמיט אותו לגמרי גרם לדוח
    להיראות קטן מהחודש שהוא מתאר, וזו הסיבה שהוא מופיע כאן — סניף אחר סניף,
    עם הפירוט והסיכום של כל אחד, כדי שהקורא ימצא את הסניף שלו באותה דרך שהוא
    מוצא אותו למעלה.
    """
    undoc = getattr(report, 'undocumented', None)
    if undoc is None or undoc.is_empty:
        return []

    groups = undoc.grouped(report.group_by)
    group_word = GROUP_WORD.get(report.group_by, 'קבוצה')
    out = [
        Spacer(1, 0.7 * cm),
        Paragraph(_rtl(f'תקבולים ללא מסמך פורמלי — לפי {group_word}'), styles['label']),
        Spacer(1, 0.15 * cm),
        _note('הסכומים הבאים נגבו בתקופה ולא הופק עבורם מסמך במערכת. '
                 'הם אינם מחוברים לסכומי המסמכים שלמעלה.', styles),
        Spacer(1, 0.35 * cm),
    ]

    for group in groups:
        note = []
        if group.is_unassigned:
            note = [_note(f'חיובים שלא ניתן לשייך ל{group_word} — חסר שיוך ברשומה שלהם.', styles)]
        elif group.rank == 1:
            note = [_note('הזמנות מהאתר שנשלחו ללקוח — אינן שייכות לסניף, ונרשמות תחת המותג.', styles)]
        out.append(KeepTogether([_undocumented_group_header(group, styles), *note]))
        out.append(Spacer(1, 0.15 * cm))
        out.append(_undocumented_group_table(group, report.group_by, styles))
        if group.merged:
            count = len(group.merged)
            opening = 'שורה אחת בסוגריים אוחדה' if count == 1 else f'{count} שורות בסוגריים אוחדו'
            out.append(Spacer(1, 0.12 * cm))
            out.append(_note(f'{opening} עם מסמך שהופק לאותו לקוח ולאותו סכום '
                     f'({_money(group.merged_total)}) ואינן נספרות בסיכום.', styles))
        out.append(Spacer(1, 0.55 * cm))

    # Per-group totals in one place, then the two halves of the period side by side.
    summary = [[
        _rtl_cell(group_word, styles['th'], 9.0 * cm),
        _rtl_cell('חיובים', styles['th'], 3.0 * cm),
        _rtl_cell('סה"כ', styles['th'], 6.0 * cm),
    ]]
    for group in groups:
        summary.append([
            _rtl_cell(group.title, styles['td'], 9.0 * cm),
            _rtl_cell(str(group.count), styles['td_num'], 3.0 * cm),
            _rtl_cell(_money(group.total), styles['td_num'], 6.0 * cm),
        ])
    summary.append([
        _rtl_cell('סה"כ ללא מסמך', styles['sub'], 9.0 * cm),
        _rtl_cell(str(undoc.count), styles['sub'], 3.0 * cm),
        _rtl_cell(_money(undoc.total), styles['sub'], 6.0 * cm),
    ])
    summary_table = Table(summary, colWidths=[9.0 * cm, 3.0 * cm, 6.0 * cm], hAlign='RIGHT', repeatRows=1)
    summary_table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, PANEL_BG]),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(5, 5, 5, 5)))
    out.append(KeepTogether([
        Paragraph(_rtl(f'סיכום ללא מסמך לפי {group_word}'), styles['label']),
        Spacer(1, 0.15 * cm),
        summary_table,
    ]))
    out.append(Spacer(1, 0.5 * cm))

    out.extend(_uncredited_refunds_block(undoc, styles))

    from apps.documents.register import money_lines

    grand = [
        [_rtl_cell(label, styles['label'], 8.0 * cm, pad=10),
         _rtl_cell(_money(amount), styles['value'], 4.5 * cm, pad=10)]
        for label, amount in money_lines(report) if amount or label.startswith('תקבולים')
    ]
    grand.append([
        _rtl_cell('סה"כ כסף שנכנס בתקופה', ParagraphStyle('AllInc', parent=styles['grand']), 8.0 * cm, pad=10),
        _rtl_cell(_money(report.all_income_total), styles['grand'], 4.5 * cm, pad=10),
    ])
    panel = Table(grand, colWidths=[8.0 * cm, 4.5 * cm], hAlign='RIGHT')
    panel.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, -2), PANEL_BG),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(6, 6, 10, 10)))
    out.append(KeepTogether([
        panel,
        Spacer(1, 0.2 * cm),
        _note('שים לב: אם הופק ידנית מסמך עבור חיוב שמופיע גם כאן ולא זוהה כמיזוג, '
                 'הוא נספר פעמיים בשורה האחרונה — אין במערכת קישור בין חיוב למסמך שהופק עבורו.', styles),
    ]))
    return out


INCOME_WIDTHS = [5.0 * cm, 3.3 * cm, 3.3 * cm, 3.0 * cm, 3.4 * cm]


def _income_by_group_block(report: PeriodReport, styles: dict) -> list:
    """
    The last word on how much each branch (or business) actually took in.

    Documents and charges are counted in the same row here because the reader's
    question is not which of the two a shekel arrived through. The left column
    is receipts only — a tax invoice and the receipt that settles it would
    otherwise show the branch as having taken the same money twice.
    """
    entries = report.income_by_group()
    if not entries:
        return []
    group_word = GROUP_WORD.get(report.group_by, 'לקוח עסקי')
    headers = [group_word, 'עם מסמך', 'ללא מסמך', 'זיכויים', 'סה"כ נכנס']

    data = [[
        _rtl_cell(head, styles['th'], width)
        for head, width in zip(headers, INCOME_WIDTHS)
    ]]
    def credit_cell(amount, style):
        return _rtl_cell(f'-{_money(amount)}' if amount else _money(amount), style, INCOME_WIDTHS[3])

    for entry in entries:
        data.append([
            _rtl_cell(entry['title'], styles['td'], INCOME_WIDTHS[0]),
            _rtl_cell(_money(entry['documented']), styles['td_num'], INCOME_WIDTHS[1]),
            _rtl_cell(_money(entry['undocumented']), styles['td_num'], INCOME_WIDTHS[2]),
            credit_cell(entry['credits'], styles['td_num']),
            _rtl_cell(_money(entry['total']), styles['sub'], INCOME_WIDTHS[4]),
        ])
    data.append([
        _rtl_cell('סה"כ', styles['sub'], INCOME_WIDTHS[0]),
        _rtl_cell(_money(sum(e['documented'] for e in entries)), styles['sub'], INCOME_WIDTHS[1]),
        _rtl_cell(_money(sum(e['undocumented'] for e in entries)), styles['sub'], INCOME_WIDTHS[2]),
        credit_cell(sum(e['credits'] for e in entries), styles['sub']),
        _rtl_cell(_money(sum(e['total'] for e in entries)), styles['grand'], INCOME_WIDTHS[4]),
    ])

    table = Table(data, colWidths=INCOME_WIDTHS, hAlign='RIGHT', repeatRows=1)
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, PANEL_BG]),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(6, 6, 6, 6)))

    # The one table the owner reads first; a page break through it would
    # separate a branch from its total.
    return [
        Spacer(1, 0.7 * cm),
        KeepTogether([
            Paragraph(_rtl(f'סיכום סופי לפי {group_word} — כמה כסף נכנס'), styles['label']),
            Spacer(1, 0.15 * cm),
            table,
            Spacer(1, 0.2 * cm),
            _note('"עם מסמך" — קבלות וחשבוניות מס/קבלה בלבד, בניכוי זיכויים. '
                     'חשבונית מס שטרם נגבתה אינה כאן, כדי שלא ייספר אותו כסף פעמיים.', styles),
        ]),
    ]


def _integrity_block(report: PeriodReport, styles: dict) -> list:
    """
    רצף המספרים, מספרים שלא הפכו למסמך, ומסמכים שייתכן שהופקו פעמיים.

    נספח ה׳(א)(5) מבקש מהתוכנה עצמה "בדיקת רצף המספרים העוקבים". כל סדרה נבדקת
    על כל שנת המס ולא רק על התקופה: מספר שחסר בחודש אחר הוא חור באותה סדרה.
    מספר שהוקצה לעסקה שלא הושלמה אינו חור — הוא מופיע כאן בשמו, כדי שמי שמחפש
    אותו ברשימה ימצא למה הוא לא שם.
    """
    full_width = PAGE_WIDTH - 2 * SIDE_MARGIN

    def titled(title, table, note):
        return [
            Spacer(1, 0.7 * cm),
            Paragraph(_rtl(title), styles['label']),
            Spacer(1, 0.15 * cm),
            table,
            Spacer(1, 0.1 * cm),
            _rtl_cell(note, styles['note'], full_width),
        ]

    def listing(headers, widths, rows, highlighted=()):
        data = [[_rtl_cell(text, styles['th'], width) for text, width in zip(headers, widths)], *rows]
        table = Table(data, colWidths=widths, hAlign='RIGHT', repeatRows=1)
        style = [
            ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
        ]
        style += [('BACKGROUND', (0, index), (-1, index), CREDIT_BG) for index in highlighted]
        table.setStyle(_card(style, padding=(5, 5, 5, 5)))
        return table

    out = []
    runs = [run for run in (report.continuity or []) if run.issued]
    if runs:
        widths = [2.9 * cm, 4.3 * cm, 5.0 * cm, 1.6 * cm, 4.2 * cm]
        rows, broken = [], []
        for run in runs:
            if run.complete:
                state = 'רציפה ושלמה'
            else:
                broken.append(len(rows) + 1)
                shown = ', '.join(run.missing[:4]) + (' …' if len(run.missing) > 4 else '')
                state = f'חסרים {len(run.missing)}: {shown}'
            if run.continues:
                # The run starts where the previous software's run stopped.
                state = f'{state} · {run.continues}'
            rows.append([
                _rtl_cell(run.name if run.series else f'{run.year} · ישנה', styles['td_num'], widths[0]),
                _rtl_cell(run.label, styles['td'], widths[1]),
                _rtl_cell(f'{run.first} — {run.last}', styles['td_num'], widths[2]),
                _rtl_cell(str(run.issued), styles['td_num'], widths[3]),
                _rtl_cell(state, styles['td'], widths[4]),
            ])
        out += titled(
            'רצף מספרי המסמכים',
            listing(('סדרה', 'סוג', 'טווח', 'מספרים', 'מצב'), widths, rows, broken),
            'כל סדרה נבדקת על כל שנת המס: כל מספר שהסדרה הקצתה נמצא על מסמך קיים '
            '(נספח ה׳(א)(5) להוראות ניהול פנקסי חשבונות).',
        )

    if report.void_rows:
        widths = [3.4 * cm, 2.2 * cm, 5.2 * cm, 4.6 * cm, 2.6 * cm]
        rows = [[
            _rtl_cell(row.document_number, styles['td_num'], widths[0]),
            _rtl_cell(row.document_date.strftime('%d/%m/%Y'), styles['td_num'], widths[1]),
            _rtl_cell(row.customer, styles['td'], widths[2]),
            _rtl_cell(row.void_reason, styles['td'], widths[3]),
            _rtl_cell(_money(row.total_amount), styles['td_num'], widths[4]),
        ] for row in report.void_rows]
        out += titled(
            'מספרים שלא הפכו למסמך',
            listing(('מספר', 'תאריך', 'לקוח', 'סיבה', 'סכום'), widths, rows),
            'המספר נשמר עם העסקה, ולכן אינו חור ברצף. העסקה לא הושלמה, ולכן סכומה אינו בסיכומים.',
        )

    if report.possible_duplicates:
        widths = [3.4 * cm, 3.4 * cm, 5.4 * cm, 2.2 * cm, 3.6 * cm]
        rows = [[
            _rtl_cell(pair['number'], styles['td_num'], widths[0]),
            _rtl_cell(pair['other'], styles['td_num'], widths[1]),
            _rtl_cell(pair['customer'], styles['td'], widths[2]),
            _rtl_cell(pair['date'].strftime('%d/%m/%Y'), styles['td_num'], widths[3]),
            _rtl_cell(_money(pair['amount']), styles['td_num'], widths[4]),
        ] for pair in report.possible_duplicates]
        out += titled(
            'מסמכים שייתכן שהופקו פעמיים לאותו תשלום',
            listing(('קבלת חוג', 'מסמך ידני', 'לקוח', 'תאריך', 'סכום'), widths, rows),
            'קבלת חוג ומסמך שהופק ידנית לאותו ילד ובאותו סכום בתקופה. שניהם נספרים בדוח; '
            'אם הם מתעדים תשלום אחד, מבטלים אחד מהם בהודעת זיכוי.',
        )
    return out


def generate_period_report_pdf(report: PeriodReport) -> bytes:
    _ensure_fonts_registered()
    styles = _styles()

    grouping_title = GROUP_BY_TITLES.get(report.group_by, report.group_by)
    header_line = f'דוח חשבוניות לתקופה — {report.period_label}'
    subheader_line = f'{grouping_title} · {report.scope_label}'

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=SIDE_MARGIN,
        rightMargin=SIDE_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=f'דוח חשבוניות {report.period_label}',
    )

    story = [
        Paragraph(_rtl('דוח חשבוניות לתקופה'), styles['title']),
        Paragraph(_rtl(f'קוגומלו · {report.period_label}'), styles['subtitle']),
        Paragraph(_rtl(f'קיבוץ {grouping_title} · {report.scope_label}'), styles['subtitle']),
    ]

    # מה הדוח מכסה — נאמר במפורש. דוח שסונן לסוג אחד נראה בדיוק כמו דוח מלא,
    # וקורא שלא ידע שסוננו עבורו מסמכים יסיק מהסכום מסקנה שגויה.
    if report.document_type:
        story.append(Paragraph(
            _rtl('הדוח כולל סוג מסמך אחד בלבד: '
                 + DOCUMENT_TYPE_LABELS.get(report.document_type, report.document_type)),
            styles['subtitle'],
        ))
    else:
        story.append(_rtl_cell(
            'הדוח כולל את כל המסמכים שהופקו בתקופה: ידניים, קבלות חוגים ומכירות חנות. '
            'טיוטות אינן נכללות — אינן מסמך.',
            styles['subtitle'], PAGE_WIDTH - 2 * SIDE_MARGIN,
        ))

    story.append(Spacer(1, 0.5 * cm))

    if not report.is_empty and not report.document_type:
        story.extend(_overview_panel(report, styles))

    if report.is_empty:
        # An empty period is a valid answer, not an error. Say so on the page
        # rather than handing back a blank sheet or a failure.
        story.extend([
            Spacer(1, 3 * cm),
            Paragraph(_rtl('לא נמצאו מסמכים בתקופה זו'), styles['empty']),
            Spacer(1, 0.4 * cm),
            Paragraph(
                _rtl(f'לא הופקה אף חשבונית בין {report.start.strftime("%d/%m/%Y")} '
                     f'ל־{report.end.strftime("%d/%m/%Y")}.'),
                styles['subtitle'],
            ),
            Paragraph(_rtl('סה"כ לתקופה: ' + _money(Decimal('0.00'))), styles['subtitle']),
        ])
    else:
        for group in report.groups:
            note = []
            if group.is_unassigned:
                explanation = {
                    GROUP_BY_BRANCH: (
                        'מסמכים שלא ניתן היה לשייך לסניף — לא דרך הסניף במסמך, '
                        'לא דרך הלקוח העסקי, ולא דרך המשפחה או ההרשמה של הילד.'
                    ),
                    GROUP_BY_UNIT: 'מסמכים שלא שויכו לעסק.',
                    GROUP_BY_CATEGORY: 'מסמכים שלא שויכו לקטגוריה.',
                }.get(report.group_by, 'מסמכים שהופקו ללקוחות רשומים ולא ללקוח עסקי.')
                note = [Spacer(1, 0.1 * cm), _note(explanation, styles)]
            # Keep a group's bar attached to its header row; the table itself is
            # free to break across pages and carries its headers with it.
            story.append(KeepTogether([_group_header(group, styles), *note]))
            story.append(Spacer(1, 0.15 * cm))
            story.append(_group_table(group, styles))
            story.append(Spacer(1, 0.55 * cm))

        story.append(Spacer(1, 0.2 * cm))
        story.extend(_group_totals_summary(report, styles))
        story.append(Spacer(1, 0.5 * cm))
        story.extend(_type_breakdown(report, styles))
        story.append(Spacer(1, 0.5 * cm))
        story.append(KeepTogether([
            Paragraph(_rtl('סיכום סופי'), styles['label']),
            Spacer(1, 0.15 * cm),
            _totals_panel(report, styles),
        ]))

        if len(report.currencies) > 1:
            # Mixed currencies would make a single sum meaningless; flag it
            # rather than presenting an addition that is not one.
            listed = ', '.join(sorted(report.currencies))
            story.extend([
                Spacer(1, 0.3 * cm),
                _note(f'שים לב: הדוח כולל יותר ממטבע אחד ({listed}). '
                         f'הסכומים חוברו כפי שהם ואינם מומרים.', styles),
            ])

    story.extend(_integrity_block(report, styles))
    story.extend(_undocumented_block(report, styles))
    story.extend(_income_by_group_block(report, styles))
    story.extend(_reconciliation_block(report, styles))

    context = {
        'header': header_line,
        'subheader': subheader_line,
        'footer': 'קוגומלו',
    }

    def make_canvas(*args, **kwargs):
        kwargs['report_context'] = context
        return NumberedCanvas(*args, **kwargs)

    doc.build(story, canvasmaker=make_canvas)
    return buffer.getvalue()
