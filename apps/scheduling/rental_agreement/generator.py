"""Renders the studio rental agreement PDF for a ScheduleEvent (is_studio_rental=True).

Uses reportlab + python-bidi rather than an HTML/CSS renderer (e.g. WeasyPrint)
because reportlab and python-bidi are pure-Python packages with no OS-level
dependencies, so this works unmodified on both of this project's deploy
targets (Fly.io, which has no Dockerfile today to add system libraries to,
and Vercel serverless, which has no system-package access at all).

reportlab does not implement the Unicode Bidi Algorithm itself (unlike a
browser/Pango-based renderer) — it only draws glyphs in the exact order it's
given. So every piece of Hebrew text must be bidi-reordered into visual order
*before* being handed to reportlab. That reordering must happen per
*rendered line*, not per logical paragraph: reordering a whole multi-sentence
paragraph in one pass and then letting reportlab's Paragraph auto-wrap the
(already-reordered) text would wrap it at the wrong points. So long text is
manually wrapped to the available column width first (using measured string
widths), each resulting line is bidi-reordered independently, and the lines
are rejoined with an explicit `<br/>` so Paragraph renders them as-is
without re-wrapping.
"""
from __future__ import annotations

import io
import os
from datetime import date, time as time_cls
from decimal import Decimal

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak,
)

from . import content

VAT_RATE = Decimal('0.18')

HEBREW_MONTHS = [
    'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני',
    'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר',
]

DAY_NAMES_HE = {
    0: 'ראשון', 1: 'שני', 2: 'שלישי', 3: 'רביעי', 4: 'חמישי', 5: 'שישי', 6: 'שבת',
}

PAGE_MARGIN = 1.8 * cm

_FONTS_DIR = os.path.join(os.path.dirname(__file__), 'fonts')
_FONTS_REGISTERED = False

ACCENT_ORANGE = colors.HexColor('#f4825a')


def _ensure_fonts_registered() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont('Heebo', os.path.join(_FONTS_DIR, 'Heebo-Regular.ttf')))
    pdfmetrics.registerFont(TTFont('Heebo-Bold', os.path.join(_FONTS_DIR, 'Heebo-Bold.ttf')))
    _FONTS_REGISTERED = True


def _rtl_line(text: str) -> str:
    """Bidi-reorder a single line/short string (must already fit on one line)."""
    return get_display(text)


def _rtl_wrapped(text: str, font_name: str, font_size: float, max_width: float) -> str:
    """Manually wrap `text` to max_width, bidi-reorder each resulting line
    independently, and join with <br/> for a Paragraph that must not
    re-wrap the (already visually-ordered) result."""
    words = text.split(' ')
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = ' '.join(current + [word])
        if not current or pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current.append(word)
        else:
            lines.append(' '.join(current))
            current = [word]
    if current:
        lines.append(' '.join(current))
    return '<br/>'.join(_rtl_line(line) for line in lines)


def _para(text: str, style: ParagraphStyle, max_width: float) -> Paragraph:
    return Paragraph(_rtl_wrapped(str(text), style.fontName, style.fontSize, max_width), style)


def _hebrew_month_year(d: date) -> str:
    return f'{HEBREW_MONTHS[d.month - 1]} {d.year}'


def _format_hours(start: time_cls, end: time_cls) -> str:
    return f'{start.strftime("%H:%M")}-{end.strftime("%H:%M")}'


def _parse_time(value: str) -> time_cls:
    parts = [int(p) for p in str(value).split(':')]
    while len(parts) < 3:
        parts.append(0)
    return time_cls(*parts[:3])


def _draw_decor(canvas, doc) -> None:
    """Light decorative accent circles in the top-right corner, echoing the
    reference letterhead's orange circles (no source logo image exists to
    embed faithfully, so this is a simplified brand nod, not a literal
    reproduction of the template's graphic header)."""
    canvas.saveState()
    canvas.setFillColor(ACCENT_ORANGE)
    page_width, page_height = A4
    positions = [
        (page_width - 1.4 * cm, page_height - 1.2 * cm, 0.55 * cm, 0.35),
        (page_width - 0.6 * cm, page_height - 2.0 * cm, 0.3 * cm, 0.22),
        (page_width - 2.2 * cm, page_height - 0.6 * cm, 0.22 * cm, 0.15),
    ]
    for x, y, r, alpha in positions:
        canvas.setFillAlpha(alpha)
        canvas.circle(x, y, r, stroke=0, fill=1)
    canvas.restoreState()


def _build_styles() -> dict[str, ParagraphStyle]:
    return {
        'title': ParagraphStyle('title', fontName='Heebo-Bold', fontSize=17, alignment=TA_CENTER, spaceAfter=10),
        'contact': ParagraphStyle('contact', fontName='Heebo-Bold', fontSize=9.5, alignment=TA_CENTER, spaceAfter=14),
        'signdate': ParagraphStyle('signdate', fontName='Heebo', fontSize=9.5, alignment=TA_RIGHT, spaceAfter=6),
        'heading': ParagraphStyle('heading', fontName='Heebo-Bold', fontSize=11.5, alignment=TA_RIGHT, spaceBefore=10, spaceAfter=6),
        'body': ParagraphStyle('body', fontName='Heebo', fontSize=9.5, alignment=TA_RIGHT, leading=13.5, spaceAfter=4),
        'bold': ParagraphStyle('bold', fontName='Heebo-Bold', fontSize=9.5, alignment=TA_RIGHT, leading=13.5, spaceAfter=6),
        'section_title': ParagraphStyle('section_title', fontName='Heebo-Bold', fontSize=13, alignment=TA_RIGHT, spaceBefore=4, spaceAfter=10),
        'table_header': ParagraphStyle('table_header', fontName='Heebo-Bold', fontSize=8.5, alignment=TA_CENTER),
        'table_cell': ParagraphStyle('table_cell', fontName='Heebo', fontSize=8.5, alignment=TA_CENTER),
        'table_total': ParagraphStyle('table_total', fontName='Heebo-Bold', fontSize=9, alignment=TA_CENTER),
        'sig_label': ParagraphStyle('sig_label', fontName='Heebo-Bold', fontSize=9.5, alignment=TA_CENTER),
        'sig_line': ParagraphStyle('sig_line', fontName='Heebo', fontSize=9.5, alignment=TA_CENTER),
        'sig_sub': ParagraphStyle('sig_sub', fontName='Heebo', fontSize=8.5, alignment=TA_CENTER, textColor=colors.HexColor('#6b7280')),
    }


def generate_rental_agreement_pdf(event) -> bytes:
    """Build the rental agreement PDF for a studio-rental ScheduleEvent.

    Raises ValueError if the event is missing data required by the contract
    (should not happen for rentals saved through the validated serializer,
    but guards against generating a PDF from incomplete data).
    """
    _ensure_fonts_registered()

    if not event.is_studio_rental:
        raise ValueError('האירוע אינו שכירות סטודיו')
    if not event.contract_start_date or not event.contract_end_date:
        raise ValueError('חסרים תאריכי תוקף הסכם')

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=2.4 * cm, bottomMargin=1.8 * cm,
        leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
    )
    max_width = doc.width
    s = _build_styles()

    story = []

    story.append(_para('תאריך חתימה: ______________', s['signdate'], max_width))
    story.append(_para(content.AGREEMENT_TITLE, s['title'], max_width))
    story.append(_para(
        f'סטודיו קוגומלו | דוא"ל: {content.STUDIO_EMAIL} | טלפון: {content.STUDIO_PHONE}',
        s['contact'], max_width,
    ))

    story.append(_para('שנערך ונחתם בין:', s['heading'], max_width))
    story.append(_para(f'{content.STUDIO_NAME} (ח.פ. {content.STUDIO_COMPANY_NUMBER})', s['bold'], max_width))
    story.append(_para('(להלן: "הסטודיו")', s['body'], max_width))
    story.append(Spacer(1, 6))
    story.append(_para('לבין המפעיל:', s['heading'], max_width))
    story.append(_para(f'שם המפעיל: {event.renter_name} | ת.ז: {event.renter_id_number}', s['bold'], max_width))
    story.append(_para('(להלן: "המפעיל" או "השוכר")', s['body'], max_width))
    story.append(Spacer(1, 10))

    activity_domain = event.name or ''
    story.append(_para(
        f'הסטודיו נותן בזה רשות שימוש למפעיל באולם הסטודיו, למטרת הפעלת חוג בתחום: {activity_domain}.',
        s['body'], max_width,
    ))
    story.append(Spacer(1, 8))

    story.append(_para('1. פירוט תשלומים ושעות פעילות - (טבלה שאפשר להוסיף עוד שורות)', s['heading'], max_width))
    story.extend(_build_payment_table(event, s))

    story.append(PageBreak())
    story.append(_para(content.SECTION_2_TITLE, s['section_title'], max_width))
    story.extend(_build_section_2(event, s, max_width))

    story.append(Spacer(1, 6))
    story.append(_para(content.SECTION_3_TITLE, s['section_title'], max_width))
    story.append(_para(content.SECTION_3_INTRO, s['body'], max_width))
    story.extend(_build_section_3(s, max_width))

    story.append(Spacer(1, 14))
    story.extend(_build_signature_block(event, s))

    doc.build(story, onFirstPage=_draw_decor, onLaterPages=_draw_decor)
    return buffer.getvalue()


def _build_payment_table(event, s: dict[str, ParagraphStyle]) -> list:
    col_widths = [3.1 * cm, 3.1 * cm, 3.1 * cm, 2.4 * cm, 2.6 * cm]

    def h(text, width):
        return _para(text, s['table_header'], width - 4)

    def c(text, width):
        return _para(text, s['table_cell'], width - 4)

    def t(text, width):
        return _para(text, s['table_total'], width - 4)

    rate = Decimal(str(event.price_per_session or 0))
    branch_name = event.branch.name if event.branch else '-'

    if event.event_type == 'weekly':
        headers = ['סה"כ לחודש (לפני מע"מ)', 'תעריף שעתי (לפני מע"מ)', 'שעות פעילות', 'יום', 'סניף']
        days = sorted(int(d) for d in (event.weekly_repeat_days or []))
        rows = []
        subtotal = Decimal('0')
        for dow in days:
            day_times = (event.weekly_day_times or {}).get(str(dow))
            if day_times:
                start = _parse_time(day_times['start_time'])
                end = _parse_time(day_times['end_time'])
            else:
                start, end = event.start_time, event.end_time
            monthly_total = rate * 4
            subtotal += monthly_total
            rows.append([
                t(f'₪{monthly_total:.0f}', col_widths[0]),
                c(f'₪{rate:.0f}', col_widths[1]),
                c(_format_hours(start, end), col_widths[2]),
                c(DAY_NAMES_HE[dow], col_widths[3]),
                c(branch_name, col_widths[4]),
            ])
        total_label = 'סה"כ לפני מע"מ:'
        pay_label = 'סה"כ לתשלום חודשי (כולל מע"מ):'
    else:
        headers = ['סה"כ לתשלום (לפני מע"מ)', 'תעריף שעתי (לפני מע"מ)', 'שעות פעילות', 'תאריך', 'סניף']
        subtotal = rate
        rows = [[
            t(f'₪{rate:.0f}', col_widths[0]),
            c(f'₪{rate:.0f}', col_widths[1]),
            c(_format_hours(event.start_time, event.end_time), col_widths[2]),
            c(event.event_date.strftime('%d/%m/%Y'), col_widths[3]),
            c(branch_name, col_widths[4]),
        ]]
        total_label = 'סה"כ לפני מע"מ:'
        pay_label = 'סה"כ לתשלום (כולל מע"מ):'

    total_with_vat = (subtotal * (1 + VAT_RATE)).quantize(Decimal('1'))

    header_row = [h(text, w) for text, w in zip(headers, col_widths)]
    data = [header_row] + rows
    table = Table(data, colWidths=col_widths, hAlign='CENTER')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f4f8')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dce3eb')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    totals_col0_width = sum(col_widths[:4])
    totals_data = [
        [t(f'₪{subtotal:.0f}', totals_col0_width), t(total_label, col_widths[4])],
        [t(f'₪{total_with_vat:.0f}', totals_col0_width), t(pay_label, col_widths[4])],
    ]
    totals_table = Table(totals_data, colWidths=[totals_col0_width, col_widths[4]], hAlign='CENTER')
    totals_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dce3eb')),
        ('BACKGROUND', (1, 0), (1, -1), colors.HexColor('#f0f4f8')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    return [table, Spacer(1, 4), totals_table]


def _build_section_2(event, s: dict[str, ParagraphStyle], max_width: float) -> list:
    flowables = []
    start_month = _hebrew_month_year(event.contract_start_date)
    end_month = _hebrew_month_year(event.contract_end_date)
    for i, (normal, emphasized) in enumerate(content.SECTION_2_ITEMS, start=1):
        text = normal.format(start_month=start_month, end_month=end_month)
        flowables.append(_para(f'{i}. {text}', s['body'], max_width))
        if emphasized:
            flowables.append(_para(emphasized, s['bold'], max_width))
        if i == 8:
            bullet_width = max_width - 1 * cm
            for bullet_text, is_bold in content.SECTION_2_ITEM_8_BULLETS:
                style = s['bold'] if is_bold else s['body']
                flowables.append(_para(f'• {bullet_text}', style, bullet_width))
    return flowables


def _build_section_3(s: dict[str, ParagraphStyle], max_width: float) -> list:
    flowables = []
    indented_width = max_width - 0.6 * cm
    for i, item in enumerate(content.SECTION_3_ITEMS, start=1):
        sub_heading, normal = item[0], item[1]
        emphasized = item[2] if len(item) > 2 else None
        flowables.append(_para(f'{i}. {sub_heading}', s['bold'], max_width))
        flowables.append(_para(normal, s['body'], indented_width))
        if emphasized:
            flowables.append(_para(emphasized, s['bold'], indented_width))
    flowables.append(_para(content.SIGNATURE_INTRO, s['heading'], max_width))
    return flowables


def _build_signature_block(event, s: dict[str, ParagraphStyle]) -> list:
    col_width = 8.2 * cm
    inner_width = col_width - 8

    # Column order [renter, studio] renders renter physically on the left and
    # studio on the right, matching the reference template's layout.
    data = [
        [
            _para(f'המפעיל: {event.renter_name}', s['sig_label'], inner_width),
            _para(content.SIGNATURE_STUDIO_LABEL, s['sig_label'], inner_width),
        ],
        [
            _para('______________________', s['sig_line'], inner_width),
            _para('______________________', s['sig_line'], inner_width),
        ],
        [
            _para(content.SIGNATURE_RENTER_SUBLABEL, s['sig_sub'], inner_width),
            _para(content.SIGNATURE_STUDIO_SUBLABEL, s['sig_sub'], inner_width),
        ],
    ]
    table = Table(data, colWidths=[col_width, col_width], hAlign='CENTER')
    table.setStyle(TableStyle([
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    return [table]
