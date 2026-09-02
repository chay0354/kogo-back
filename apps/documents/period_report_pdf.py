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
    _money,
    _rtl,
)

PAGE_WIDTH, PAGE_HEIGHT = A4

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

# Column widths sum to the printable width (A4 minus both margins).
COL_WIDTHS = [
    5.9 * cm,   # customer
    2.3 * cm,   # document number
    2.5 * cm,   # document type
    2.0 * cm,   # date
    2.1 * cm,   # net (before VAT)
    1.9 * cm,   # VAT
    2.3 * cm,   # total
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


def _rtl_cell(text: str, style: ParagraphStyle, width: float) -> Paragraph:
    """
    Wrap to `width` first, then bidi-reorder each line on its own.

    Reordering the whole string and letting Paragraph wrap it afterwards would
    wrap visually-ordered text at logical word boundaries and scramble a long
    Hebrew name. Padding of 8pt matches the table's cell padding.
    """
    text = str(text or '')
    usable = max(width - 8, 10)
    words = text.split(' ')
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
    st = styles[style_key]
    return [
        _rtl_cell(label, st, COL_WIDTHS[0]),
        _rtl_cell('', st, COL_WIDTHS[1]),
        _rtl_cell('', st, COL_WIDTHS[2]),
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
    for index in band_rows:
        style.append(('SPAN', (0, index), (-1, index)))
        style.append(('BACKGROUND', (0, index), (-1, index), SECTION_BG))
    for index in subtotal_rows:
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
        [_rtl_cell(label, styles['label'], 8.0 * cm),
         _rtl_cell(value, styles['value'], 4.5 * cm)]
        for label, value in rows
    ]
    net_label = 'סה"כ נטו לתקופה (חיובים בניכוי זיכויים)' if totals.credits_total > 0 else 'סה"כ לתקופה'
    data.append([
        _rtl_cell(net_label, ParagraphStyle('GrandL', parent=styles['grand']), 8.0 * cm),
        _rtl_cell(_money(totals.net_of_credits), styles['grand'], 4.5 * cm),
    ])

    table = Table(data, colWidths=[8.0 * cm, 4.5 * cm], hAlign='RIGHT')
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, -2), PANEL_BG),
        ('BACKGROUND', (0, -1), (-1, -1), SUBTOTAL_BG),
        ('LINEABOVE', (0, -1), (-1, -1), 1, BRAND_PURPLE),
    ], padding=(6, 6, 10, 10)))
    return table


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
        Spacer(1, 0.5 * cm),
    ]

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
                note = [Spacer(1, 0.1 * cm), Paragraph(_rtl(explanation), styles['note'])]
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
        story.append(Paragraph(_rtl('סיכום סופי'), styles['label']))
        story.append(Spacer(1, 0.15 * cm))
        story.append(_totals_panel(report, styles))

        if len(report.currencies) > 1:
            # Mixed currencies would make a single sum meaningless; flag it
            # rather than presenting an addition that is not one.
            listed = ', '.join(sorted(report.currencies))
            story.extend([
                Spacer(1, 0.3 * cm),
                Paragraph(
                    _rtl(f'שים לב: הדוח כולל יותר ממטבע אחד ({listed}). '
                         f'הסכומים חוברו כפי שהם ואינם מומרים.'),
                    styles['note'],
                ),
            ])

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
