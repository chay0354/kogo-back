"""Renders the studio rental agreement PDF from a contract's terms.

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

There is one renderer, generate_tenancy_contract_pdf(terms), and it reads no
model: it draws what the terms (terms.py) say. A tenancy's stored contracts are
drawn with it (apps/rentals/contracts.py), and so is the calendar's per-event
download: generate_rental_agreement_pdf(event) builds the terms of its one
event and hands them over, so the two documents cannot drift apart. The signed
copy of a contract is the same drawing with the tenant's signature in the
signature block (`signed=SignedBy(...)`), and text.py writes the same contract
out as plain paragraphs for the signing page, from the same helpers below.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak,
)

from . import content
from .terms import KIND_ONE_TIME, KIND_WEEKLY, PERIOD_ONCE, event_terms, terms_sha256

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
MUTED_GRAY = colors.HexColor('#6b7280')

# How much of terms_sha256 the foot of each page of a stored contract prints:
# enough to tell two versions apart at a glance, short enough to read aloud.
FOOTER_HASH_CHARS = 12

# The signed copy states the time of signing in Israel, whatever zone the server runs in.
ISRAEL_TZ = ZoneInfo('Asia/Jerusalem')
# The box the tenant's signature is scaled into, inside the renter's column.
SIGNATURE_IMAGE_MAX_WIDTH = 6.0 * cm
SIGNATURE_IMAGE_MAX_HEIGHT = 2.2 * cm

PAYMENT_TABLE_HEADING = '1. פירוט תשלומים ושעות פעילות - (טבלה שאפשר להוסיף עוד שורות)'
STUDIO_PARTY_HEADING = 'שנערך ונחתם בין:'
STUDIO_PARTY_ALIAS = '(להלן: "הסטודיו")'
TENANT_PARTY_HEADING = 'לבין המפעיל:'
TENANT_PARTY_ALIAS = '(להלן: "המפעיל" או "השוכר")'


@dataclass(frozen=True)
class SignedBy:
    """The tenant's signature, as the signed copy draws it into the signature block."""

    png: bytes
    name: str
    id_number: str
    signed_at: datetime
    signature_id: str
    terms_sha256: str

    def local_time(self, fmt: str) -> str:
        return self.signed_at.astimezone(ISRAEL_TZ).strftime(fmt)


def studio_contact_line(studio: dict) -> str:
    return f'סטודיו קוגומלו | דוא"ל: {studio["email"]} | טלפון: {studio["phone"]}'


def studio_party_line(studio: dict) -> str:
    return f'{studio["name"]} (ח.פ. {studio["company_number"]})'


def activity_line(terms: dict) -> str:
    return f'הסטודיו נותן בזה רשות שימוש למפעיל באולם הסטודיו, למטרת הפעלת חוג בתחום: {terms["activity"]}.'


def row_when(row: dict) -> str:
    """A payment-table row's day: the weekday of a weekly slot, the date of a one-time rental."""
    return DAY_NAMES_HE[row['weekday']] if row['kind'] == KIND_WEEKLY else _format_date(row['date'])


def row_hours(row: dict) -> str:
    return _format_hours(row['start_time'], row['end_time'])


def row_place(row: dict, terms: dict) -> str:
    branch_name = (terms.get('branch') or {}).get('name') or '-'
    return f'{branch_name} · {row["studio"]}' if row.get('studio') else branch_name


def shekels(amount) -> str:
    return _shekels(amount)


def vat_label(terms: dict) -> str:
    return f'מע"מ {_percent(terms["vat_rate"])}%:'


def pay_label(terms: dict) -> str:
    return 'סה"כ לתשלום (כולל מע"מ):' if terms['period'] == PERIOD_ONCE else 'סה"כ לתשלום חודשי (כולל מע"מ):'


def section_2_items(terms: dict) -> list[tuple[str, str | None, list[tuple[str, bool]]]]:
    """Section 2 as (numbered text, its emphasized line or None, its bullets), with the months filled in."""
    start_month = _hebrew_month_year(date.fromisoformat(terms['start_date']))
    end_month = _hebrew_month_year(date.fromisoformat(terms['end_date']))
    items = []
    for i, (normal, emphasized) in enumerate(content.SECTION_2_ITEMS, start=1):
        text = normal.format(start_month=start_month, end_month=end_month)
        bullets = list(content.SECTION_2_ITEM_8_BULLETS) if i == 8 else []
        items.append((f'{i}. {text}', emphasized, bullets))
    return items


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
    re-wrap the (already visually-ordered) result.

    Each line is XML-escaped after it is measured and reordered: Paragraph
    reads its text as markup, and a tenant's name or address may hold & or <."""
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
    return '<br/>'.join(escape(_rtl_line(line)) for line in lines)


def _para(text: str, style: ParagraphStyle, max_width: float) -> Paragraph:
    return Paragraph(_rtl_wrapped(str(text), style.fontName, style.fontSize, max_width), style)


def _hebrew_month_year(d: date) -> str:
    return f'{HEBREW_MONTHS[d.month - 1]} {d.year}'


def _format_hours(start: str | None, end: str | None) -> str:
    if not start or not end:
        return '-'
    return f'{start}-{end}'


def _format_date(value: str) -> str:
    return date.fromisoformat(value).strftime('%d/%m/%Y')


def _shekels(amount) -> str:
    # To the agora, never rounded to whole shekels: the contract states the
    # amount that will actually be charged (Tenancy.monthly_total, which
    # apps.core.vat.add_vat computes to the agora), so rounding here would put
    # one number on paper and charge another.
    return f'₪{Decimal(str(amount)):.2f}'


def _percent(rate) -> str:
    """'0.18' -> '18'."""
    return format((Decimal(str(rate)) * 100).normalize(), 'f')


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


def contract_footer_text(version: int, terms: dict) -> str:
    """The line at the foot of each page of a stored contract: its version and the start of its terms fingerprint."""
    return f'גרסת חוזה {version} · מזהה תנאים {terms_sha256(terms)[:FOOTER_HASH_CHARS]}'


def _page_decorator(footer: str | None):
    """The page callback: the corner accent on every page, and the footer line when there is one."""

    def decorate(canvas, doc) -> None:
        _draw_decor(canvas, doc)
        if footer:
            canvas.saveState()
            canvas.setFont('Heebo', 7)
            canvas.setFillColor(MUTED_GRAY)
            # Inside the bottom margin, below anything the story can reach.
            canvas.drawCentredString(A4[0] / 2, 1.0 * cm, _rtl_line(footer))
            canvas.restoreState()

    return decorate


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
        'sig_proof': ParagraphStyle('sig_proof', fontName='Heebo', fontSize=7, alignment=TA_CENTER, leading=9.5, textColor=MUTED_GRAY),
    }


def generate_rental_agreement_pdf(event) -> bytes:
    """Build the rental agreement PDF for a studio-rental ScheduleEvent.

    Raises ValueError if the event is missing data required by the contract
    (should not happen for rentals saved through the validated serializer,
    but guards against generating a PDF from incomplete data).
    """
    return generate_tenancy_contract_pdf(event_terms(event))


def generate_tenancy_contract_pdf(
    terms: dict, *, version: int | None = None, signed: SignedBy | None = None,
) -> bytes:
    """Draw a rental contract from its terms (terms.py) and return the PDF bytes.

    `version` is the stored contract's number. With it, every page carries a
    small line with the version and the first characters of terms_sha256, so a
    printed page names the exact terms it belongs to. The calendar's per-event
    download is no stored contract and passes none.

    `signed` draws the signed copy: the date of signing at the top, and in the
    tenant's column of the signature block their signature, name, ID and the
    time, with the terms fingerprint and the signature's id beneath. Everything
    else is the contract exactly as it was issued.
    """
    _ensure_fonts_registered()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=2.4 * cm, bottomMargin=1.8 * cm,
        leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
    )
    max_width = doc.width
    s = _build_styles()
    studio = terms['studio']
    tenant = terms['tenant']

    story = []

    sign_date = signed.local_time('%d/%m/%Y') if signed else '______________'
    story.append(_para(f'תאריך חתימה: {sign_date}', s['signdate'], max_width))
    story.append(_para(content.AGREEMENT_TITLE, s['title'], max_width))
    story.append(_para(studio_contact_line(studio), s['contact'], max_width))

    story.append(_para(STUDIO_PARTY_HEADING, s['heading'], max_width))
    story.append(_para(studio_party_line(studio), s['bold'], max_width))
    story.append(_para(STUDIO_PARTY_ALIAS, s['body'], max_width))
    story.append(Spacer(1, 6))
    story.append(_para(TENANT_PARTY_HEADING, s['heading'], max_width))
    story.append(_para(tenant_line(tenant), s['bold'], max_width))
    contact = tenant_contact_line(tenant)
    if contact:
        story.append(_para(contact, s['body'], max_width))
    story.append(_para(TENANT_PARTY_ALIAS, s['body'], max_width))
    story.append(Spacer(1, 10))

    story.append(_para(activity_line(terms), s['body'], max_width))
    story.append(Spacer(1, 8))

    story.append(_para(PAYMENT_TABLE_HEADING, s['heading'], max_width))
    story.extend(_build_payment_table(terms, s))

    story.append(PageBreak())
    story.append(_para(content.SECTION_2_TITLE, s['section_title'], max_width))
    story.extend(_build_section_2(terms, s, max_width))

    story.append(Spacer(1, 6))
    story.append(_para(content.SECTION_3_TITLE, s['section_title'], max_width))
    story.append(_para(content.SECTION_3_INTRO, s['body'], max_width))
    story.extend(_build_section_3(s, max_width))

    story.append(Spacer(1, 14))
    story.extend(_build_signature_block(tenant['name'], s, signed))

    decorate = _page_decorator(contract_footer_text(version, terms) if version is not None else None)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


def tenant_line(tenant: dict) -> str:
    """'שם המפעיל: … | ת.ז: …', with the company number (ח.פ) for a company."""
    parts = [f'שם המפעיל: {tenant["name"]}']
    if tenant.get('company_number'):
        parts.append(f'ח.פ: {tenant["company_number"]}')
    if tenant.get('id_number') or not tenant.get('company_number'):
        # The calendar's line has always named an ID, even an empty one.
        parts.append(f'ת.ז: {tenant.get("id_number", "")}')
    return ' | '.join(parts)


def tenant_contact_line(tenant: dict) -> str:
    """The tenant's phone, email and address, those that are known. Empty when none is."""
    parts = [
        f'{label} {tenant[key]}'
        for key, label in (('phone', 'טלפון:'), ('email', 'דוא"ל:'), ('address', 'כתובת:'))
        if tenant.get(key)
    ]
    return ' | '.join(parts)


def _build_payment_table(terms: dict, s: dict[str, ParagraphStyle]) -> list:
    col_widths = [3.1 * cm, 3.1 * cm, 3.1 * cm, 2.4 * cm, 2.6 * cm]

    def h(text, width):
        return _para(text, s['table_header'], width - 4)

    def c(text, width):
        return _para(text, s['table_cell'], width - 4)

    def t(text, width):
        return _para(text, s['table_total'], width - 4)

    rows = terms['rows']
    kinds = {row['kind'] for row in rows}
    if kinds == {KIND_ONE_TIME}:
        sum_header, when_header = 'סה"כ לתשלום (לפני מע"מ)', 'תאריך'
    elif kinds <= {KIND_WEEKLY}:
        sum_header, when_header = 'סה"כ לחודש (לפני מע"מ)', 'יום'
    else:
        # A tenancy may hold a one-time rental beside its weekly slots: each row
        # still says which it is, by a weekday or by a date.
        sum_header, when_header = 'סה"כ (לפני מע"מ)', 'יום / תאריך'
    with_studio = any(row.get('studio') for row in rows)
    headers = [
        sum_header, 'תעריף שעתי (לפני מע"מ)', 'שעות פעילות', when_header,
        'סניף / סטודיו' if with_studio else 'סניף',
    ]
    body = []
    for row in rows:
        body.append([
            t(_shekels(row['sum']), col_widths[0]),
            c(_shekels(row['rate']), col_widths[1]),
            c(row_hours(row), col_widths[2]),
            c(row_when(row), col_widths[3]),
            c(row_place(row, terms), col_widths[4]),
        ])

    header_row = [h(text, w) for text, w in zip(headers, col_widths)]
    data = [header_row] + body
    table = Table(data, colWidths=col_widths, hAlign='CENTER')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f0f4f8')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dce3eb')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    totals_col0_width = sum(col_widths[:4])
    # The terms' own amount, VAT and total, not the rows added up: a tenancy's
    # contract states the monthly amount the office agreed on, which may differ
    # from rate × 4 per row (apps/rentals/contracts.py build_terms).
    totals_data = [
        [t(_shekels(terms['monthly_amount']), totals_col0_width), t('סה"כ לפני מע"מ:', col_widths[4])],
        [t(_shekels(terms['vat_amount']), totals_col0_width), t(vat_label(terms), col_widths[4])],
        [t(_shekels(terms['monthly_total']), totals_col0_width), t(pay_label(terms), col_widths[4])],
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


def _build_section_2(terms: dict, s: dict[str, ParagraphStyle], max_width: float) -> list:
    flowables = []
    bullet_width = max_width - 1 * cm
    for text, emphasized, bullets in section_2_items(terms):
        flowables.append(_para(text, s['body'], max_width))
        if emphasized:
            flowables.append(_para(emphasized, s['bold'], max_width))
        for bullet_text, is_bold in bullets:
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


def _signature_image(png: bytes) -> Image:
    """The tenant's signature scaled into its box. The signing checks the image reads before it gets here."""
    width_px, height_px = ImageReader(io.BytesIO(png)).getSize()
    scale = min(SIGNATURE_IMAGE_MAX_WIDTH / width_px, SIGNATURE_IMAGE_MAX_HEIGHT / height_px)
    return Image(io.BytesIO(png), width=width_px * scale, height=height_px * scale)


def signed_by_lines(signed: SignedBy) -> list[str]:
    """
    Who signed and when, then what exactly: the lines under the tenant's signature on the signed copy.

    Plain "label: value", like the contract's other lines: bidi reordering moves
    brackets and trailing dots around a Latin run, so the hash is not wrapped in any.
    """
    return [
        f'{signed.name} · ת.ז./ח.פ: {signed.id_number}',
        f'נחתם ב־{signed.local_time("%d/%m/%Y %H:%M")} (שעון ישראל)',
        f'מזהה תנאים: {signed.terms_sha256}',
        f'מזהה חתימה: {signed.signature_id}',
    ]


def _build_signature_block(renter_name: str, s: dict[str, ParagraphStyle], signed: SignedBy | None = None) -> list:
    col_width = 8.2 * cm
    inner_width = col_width - 8

    # Column order [renter, studio] renders renter physically on the left and
    # studio on the right, matching the reference template's layout.
    renter_mark = _signature_image(signed.png) if signed else _para('______________________', s['sig_line'], inner_width)
    data = [
        [
            _para(f'המפעיל: {renter_name}', s['sig_label'], inner_width),
            _para(content.SIGNATURE_STUDIO_LABEL, s['sig_label'], inner_width),
        ],
        [
            renter_mark,
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
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ]))
    if not signed:
        return [table]
    # The signer, the time and the fingerprints, the full width under the
    # block: the hash is 64 characters and would not fit in one column.
    proof = [_para(line, s['sig_proof'], 2 * col_width) for line in signed_by_lines(signed)]
    return [table, Spacer(1, 6), *proof]
