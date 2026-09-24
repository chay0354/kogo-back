"""The one drawing of a customer-facing Cogomelo document.

Every PDF a customer sees — the receipt for a lesson charge, a store sale, a
document issued by hand — is drawn by :func:`render_invoice_pdf` from the plain
:class:`InvoiceLayout` structure below. The three generators decide *what* a
document says; this module is the only place that decides how it looks, so they
cannot drift apart.

The design is the owner's own mock-up (``design/invoice-sample.pdf``): a warm
page with a yellow corner swoosh, the logo centred, the document's name and
number large beneath it, one card holding the document/customer details beside
the business details, a purple-headed table of the transaction, the payment
details beside a totals card, the small print, and a footer line.

Nothing here knows about a model. Rows whose value is empty are dropped rather
than printed as a bare label, which is what lets a document from 2024 — no
company number, no order number, no card digits — render without holes.

Hebrew RTL: text is wrapped to the column width *first* and each resulting line
is bidi-reordered on its own (the technique of ``period_report_pdf._rtl_cell``
and ``signatures.pdf._wrapped``). Reordering a whole string and letting
Paragraph wrap the visually-ordered result breaks lines at the wrong points and
scrambles a long Hebrew name.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal
from xml.sax.saxutils import escape

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_NAME
from apps.documents.signature_seal import SignatureSeal

# --- the palette, taken from the sample -------------------------------------
# Sampled out of design/invoice-sample.pdf's content stream; named once here so
# no generator hard-codes a colour of its own.

PAGE_BG = colors.HexColor('#fbf7f1')        # the warm page
CARD_BG = colors.HexColor('#f8f8ff')        # the rounded cards
CARD_BORDER = colors.HexColor('#cfd1ef')    # their 1pt lavender edge
TABLE_HEAD_BG = colors.HexColor('#35379e')  # the purple header row
TABLE_HEAD_SEP = colors.HexColor('#4b4eb0')  # column dividers inside it
ROW_SEP = colors.HexColor('#dedff0')        # dividers in the body
TITLE_COLOR = colors.HexColor('#34389b')
HEADING_COLOR = colors.HexColor('#27346f')
LABEL_COLOR = colors.HexColor('#25346f')
VALUE_COLOR = colors.HexColor('#56618d')
BODY_TEXT = colors.HexColor('#2d396d')
SUB_TEXT = colors.HexColor('#8a93ac')
GRAND_TOTAL_COLOR = colors.HexColor('#31339d')
NOTE_STRONG = colors.HexColor('#56618d')
NOTE_TEXT = colors.HexColor('#6d7187')
ACCENT_NOTE = colors.HexColor('#8a6e32')    # the olive note under the payment block
FOOTER_TEXT = colors.HexColor('#55648d')
RULE_GREY = colors.HexColor('#dedede')
RULE_CYAN = colors.HexColor('#36bfe8')
MARK_YELLOW = colors.HexColor('#f4bb30')    # the corner swoosh and ring

# --- the geometry, in points, measured off the sample ------------------------

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 43.2
CONTENT_RIGHT = 551.5
CONTENT_WIDTH = CONTENT_RIGHT - MARGIN_X       # 508.3

CARD_RADIUS = 12
CARD_PADDING = 16
TABLE_RADIUS = 10

# The business half is the wider one: its address has to stay on one line
# (a wrapped address is legal but reads badly on a document).
BUSINESS_HALF = 0.53

LOGO_WIDTH = 136.0
LOGO_HEIGHT = 59.5
LOGO_TOP = 42.5

FOOTER_RULE_Y = 814.9          # from the top of the page
FOOTER_TEXT_TOP = 817.8
BODY_TOP = 131.6               # where the story starts (title glyphs land at ~137)
BODY_BOTTOM = 800.0            # the story may not run past this

# The label column of a card sizes itself to its widest label, up to this much
# of the block — a card with no long label stays as tight as the sample's.
LABEL_COL_MAX_SHARE = 0.5
LABEL_COL_GUTTER = 5.0

# The signed original's seal, and the column it takes at the left of the small print.
SEAL_DIAMETER = 66.0
SEAL_COLUMN = SEAL_DIAMETER + 18.0

# The transaction table's six columns, left to right in page order. The widths
# are the sample's, measured between its column dividers.
ITEM_COL_WIDTHS = (81.7, 50.7, 86.7, 86.4, 45.8, 157.0)

# --- fonts -------------------------------------------------------------------
# The sample embeds Noto Sans Hebrew / Arimo as subsets, which cannot be reused
# for arbitrary text. Heebo — already in the repo, and the same geometric Hebrew
# sans — stands in for it.

_FONTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'scheduling', 'rental_agreement', 'fonts',
)
_FONTS_REGISTERED = False

LOGO_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'kogo-logo.png')

# Both Heebo files in the repo carry the same internal PostScript name,
# "Heebo-Regular". reportlab keys dynamic fonts by that name (pdfmetrics
# registerFont -> _dynFaceNames), so registering the bold file under the plain
# name "Heebo-Bold" silently hands back the regular face and every bold line
# comes out light. The bold face is therefore given a name of its own before it
# is registered, under a font name no other module uses — so the fix holds
# whichever module registers its fonts first in the process.
FONT_REGULAR = 'Heebo'
FONT_BOLD = 'KogoHeebo-Bold'


def ensure_fonts_registered() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont(FONT_REGULAR, os.path.join(_FONTS_DIR, 'Heebo-Regular.ttf')))
    bold = TTFont(FONT_BOLD, os.path.join(_FONTS_DIR, 'Heebo-Bold.ttf'))
    bold.face.name = b'Heebo-Bold'
    bold.face.fullName = b'Heebo Bold'
    pdfmetrics.registerFont(bold)
    # So <b> inside a Paragraph reaches the bold face.
    pdfmetrics.registerFontFamily(
        FONT_REGULAR, normal=FONT_REGULAR, bold=FONT_BOLD,
        italic=FONT_REGULAR, boldItalic=FONT_BOLD,
    )
    _FONTS_REGISTERED = True


def rtl(text: str) -> str:
    """Bidi-reorder one short string that is known to fit on a single line."""
    return get_display(str(text or ''))


def money(amount: Decimal | float | int | str) -> str:
    """₪ with two decimals, minus sign kept in front for a credit."""
    value = Decimal(str(amount if amount not in (None, '') else 0))
    if value < 0:
        return f'-₪{abs(value):.2f}'
    return f'₪{value:.2f}'


# --- RTL-safe paragraphs ------------------------------------------------------

def _split_long_word(word: str, font_name: str, font_size: float, usable: float) -> list[str]:
    """Break a single word too wide for the column (a long email, a URL)."""
    parts: list[str] = []
    current = ''
    for char in word:
        if current and pdfmetrics.stringWidth(current + char, font_name, font_size) > usable:
            parts.append(current)
            current = char
        else:
            current += char
    if current:
        parts.append(current)
    return parts or ['']


def wrapped_lines(
    text: str, font_name: str, font_size: float, usable: float,
    first_usable: float | None = None,
) -> list[str]:
    """
    The logical lines `text` breaks into inside `usable` points.

    `first_usable` narrows the first line only — the small print's bold lead-in
    shares that line, so the rest of it has less room than the ones below.
    """
    lines: list[str] = []
    for source_line in str(text or '').splitlines() or ['']:
        current: list[str] = []
        for word in source_line.split(' '):
            room = usable if (lines or current) else (first_usable or usable)
            for piece in (
                [word]
                if pdfmetrics.stringWidth(word, font_name, font_size) <= room
                else _split_long_word(word, font_name, font_size, room)
            ):
                candidate = ' '.join(current + [piece])
                width_now = usable if lines else (first_usable or usable)
                if not current or pdfmetrics.stringWidth(candidate, font_name, font_size) <= width_now:
                    current.append(piece)
                else:
                    lines.append(' '.join(current))
                    current = [piece]
        lines.append(' '.join(current))
    return lines or ['']


def para(text: str, style: ParagraphStyle, width: float) -> Paragraph:
    """
    Wrap to `width` first, reorder each line on its own, escape, then join.

    Lines are measured a few points short so Paragraph never re-wraps one: in
    visual order the logical first word sits at the end of the string, so a
    re-wrap would push exactly the wrong word onto a line of its own.
    """
    usable = max(width - 4, 10)
    lines = wrapped_lines(text, style.fontName, style.fontSize, usable)
    return Paragraph('<br/>'.join(escape(get_display(line)) for line in lines), style)


# --- the data a document hands to the drawing ---------------------------------

@dataclass
class Field:
    """One label/value row. Dropped when the value is empty."""
    label: str
    value: str = ''

    @property
    def filled(self) -> bool:
        return str(self.value).strip() != ''


@dataclass
class LineItem:
    """One row of פירוט העסקה. Every column is pre-formatted text."""
    description: str
    sub: str = ''            # the small grey line under the description
    quantity: str = ''
    unit_price: str = ''     # מחיר יחידה לפני מע"מ
    line_net: str = ''       # סה"כ לפני מע"מ
    vat_rate: str = ''       # מע"מ
    line_gross: str = ''     # סה"כ כולל מע"מ


@dataclass
class Note:
    """A line of small print: a bold lead-in and the rest."""
    lead: str = ''
    text: str = ''


@dataclass
class InvoiceLayout:
    title: str                                   # "חשבונית מס/קבלה - IR-2026-000123"
    copy_mark: str = 'מקור'
    document_fields: list[Field] = dataclass_field(default_factory=list)
    business_fields: list[Field] = dataclass_field(default_factory=list)
    document_heading: str = 'פרטי המסמך והלקוח'
    business_heading: str = 'פרטי העסק'
    items: list[LineItem] = dataclass_field(default_factory=list)
    items_heading: str = 'פירוט העסקה'
    payment_heading: str = 'פרטי תשלום'
    payment_fields: list[Field] = dataclass_field(default_factory=list)
    payment_note: str = ''
    totals: list[Field] = dataclass_field(default_factory=list)
    grand_label: str = 'סה"כ לתשלום'
    grand_value: str = ''
    notes: list[Note] = dataclass_field(default_factory=list)
    footer: str = ''
    watermark: str = ''
    # The round seal beside the small print — on the signed original only.
    signed_seal: bool = False
    pdf_title: str = ''
    pdf_author: str = ''


# --- styles -------------------------------------------------------------------

def _styles() -> dict[str, ParagraphStyle]:
    return {
        'title': ParagraphStyle(
            'InvTitle', fontName=FONT_BOLD, fontSize=18.5, leading=24,
            textColor=TITLE_COLOR, alignment=TA_CENTER,
        ),
        'copy_mark': ParagraphStyle(
            'InvCopyMark', fontName=FONT_BOLD, fontSize=9.75, leading=14,
            textColor=HEADING_COLOR, alignment=TA_CENTER,
        ),
        'heading': ParagraphStyle(
            'InvHeading', fontName=FONT_BOLD, fontSize=9, leading=12,
            textColor=HEADING_COLOR, alignment=TA_RIGHT,
        ),
        'label': ParagraphStyle(
            'InvLabel', fontName=FONT_BOLD, fontSize=8.25, leading=13,
            textColor=LABEL_COLOR, alignment=TA_RIGHT,
        ),
        'value': ParagraphStyle(
            'InvValue', fontName=FONT_REGULAR, fontSize=8.25, leading=13,
            textColor=VALUE_COLOR, alignment=TA_LEFT,
        ),
        'th': ParagraphStyle(
            'InvTh', fontName=FONT_BOLD, fontSize=7.9, leading=10.5,
            textColor=colors.white, alignment=TA_CENTER,
        ),
        'th_right': ParagraphStyle(
            'InvThRight', fontName=FONT_BOLD, fontSize=7.9, leading=10.5,
            textColor=colors.white, alignment=TA_RIGHT,
        ),
        'td': ParagraphStyle(
            'InvTd', fontName=FONT_REGULAR, fontSize=7.9, leading=11,
            textColor=BODY_TEXT, alignment=TA_RIGHT,
        ),
        'td_num': ParagraphStyle(
            'InvTdNum', fontName=FONT_REGULAR, fontSize=7.9, leading=11,
            textColor=BODY_TEXT, alignment=TA_CENTER,
        ),
        'td_bold': ParagraphStyle(
            'InvTdBold', fontName=FONT_BOLD, fontSize=7.9, leading=11,
            textColor=BODY_TEXT, alignment=TA_CENTER,
        ),
        'td_sub': ParagraphStyle(
            'InvTdSub', fontName=FONT_REGULAR, fontSize=7.1, leading=10,
            textColor=SUB_TEXT, alignment=TA_RIGHT,
        ),
        'grand_label': ParagraphStyle(
            'InvGrandLabel', fontName=FONT_BOLD, fontSize=11.25, leading=15,
            textColor=HEADING_COLOR, alignment=TA_RIGHT,
        ),
        'grand_value': ParagraphStyle(
            'InvGrandValue', fontName=FONT_BOLD, fontSize=17.25, leading=22,
            textColor=GRAND_TOTAL_COLOR, alignment=TA_LEFT,
        ),
        'payment_note': ParagraphStyle(
            'InvPaymentNote', fontName=FONT_REGULAR, fontSize=7.1, leading=10,
            textColor=ACCENT_NOTE, alignment=TA_RIGHT,
        ),
        'note': ParagraphStyle(
            'InvNote', fontName=FONT_REGULAR, fontSize=7.1, leading=12.1,
            textColor=NOTE_TEXT, alignment=TA_RIGHT,
        ),
    }


# --- the painted parts of the page -------------------------------------------

def _y(top_distance: float) -> float:
    """A distance measured from the top of the page, as a PDF y coordinate."""
    return PAGE_HEIGHT - top_distance


def _draw_corner_marks(canvas) -> None:
    """The yellow swoosh top-left and the ring top-right."""
    canvas.saveState()
    canvas.setStrokeColor(MARK_YELLOW)
    canvas.setLineCap(1)

    # The bold strand is an arc of a circle fitted to the three points the
    # sample's swoosh passes through; the two thin ones fan out beneath it.
    canvas.setLineWidth(3.0)
    canvas.arc(-13.93, _y(20.82), 40.79, _y(-33.9), -119.4, 105.6)

    canvas.setLineWidth(1.5)
    canvas.bezier(24.5, _y(20.5), 19.0, _y(24.2), 9.0, _y(27.6), 0.0, _y(27.2))
    canvas.bezier(24.5, _y(20.5), 19.5, _y(27.5), 9.0, _y(34.0), 0.0, _y(35.0))

    canvas.setLineWidth(1.45)
    canvas.circle(551.4, _y(32.5), 9.4, stroke=1, fill=0)
    canvas.setFillColor(MARK_YELLOW)
    canvas.circle(559.0, _y(24.6), 0.85, stroke=0, fill=1)
    canvas.restoreState()


def _draw_logo(canvas) -> None:
    """The logo, centred. A missing or unreadable file must never break a document."""
    try:
        if not os.path.isfile(LOGO_PATH):
            return
        canvas.drawImage(
            LOGO_PATH,
            (PAGE_WIDTH - LOGO_WIDTH) / 2,
            _y(LOGO_TOP + LOGO_HEIGHT),
            width=LOGO_WIDTH,
            height=LOGO_HEIGHT,
            preserveAspectRatio=True,
            anchor='c',
            mask='auto',
        )
    except Exception:  # a corrupt PNG, a locked file — the document still goes out
        return


def _draw_footer(canvas, text: str, page_number: int) -> None:
    canvas.saveState()
    canvas.setStrokeColor(RULE_CYAN)
    canvas.setLineWidth(0.85)
    canvas.line(MARGIN_X, _y(FOOTER_RULE_Y), CONTENT_RIGHT, _y(FOOTER_RULE_Y))
    if text:
        canvas.setFont(FONT_REGULAR, 12)
        canvas.setFillColor(FOOTER_TEXT)
        canvas.drawCentredString(PAGE_WIDTH / 2, _y(FOOTER_TEXT_TOP + 11.5), rtl(text))
    # Only a document that actually runs over says which page you are holding;
    # page one then stays exactly the sample.
    if page_number > 1:
        canvas.setFont(FONT_REGULAR, 7.1)
        canvas.setFillColor(SUB_TEXT)
        canvas.drawString(MARGIN_X, _y(FOOTER_RULE_Y - 6), rtl(f'עמוד {page_number}'))
    canvas.restoreState()


def _draw_watermark(canvas, text: str) -> None:
    canvas.saveState()
    canvas.setFont(FONT_BOLD, 92)
    canvas.setFillColor(colors.Color(0.55, 0.55, 0.65, alpha=0.25))
    canvas.translate(PAGE_WIDTH / 2, PAGE_HEIGHT / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, 0, rtl(text))
    canvas.restoreState()


def _page_painter(layout: InvoiceLayout):
    def on_page(canvas, document):
        canvas.saveState()
        canvas.setFillColor(PAGE_BG)
        canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
        canvas.restoreState()
        _draw_corner_marks(canvas)
        _draw_logo(canvas)
        _draw_footer(canvas, layout.footer, document.page)
    return on_page


def _canvas_maker(layout: InvoiceLayout):
    """
    A canvas that stamps the watermark last.

    The page callbacks run before the flowables, so a watermark drawn there
    disappears behind the opaque cards. Drawing it as the page closes puts it
    over the whole document, which is the only way a draft reads as one.
    """
    if not layout.watermark:
        return Canvas

    class WatermarkedCanvas(Canvas):
        def showPage(self):
            _draw_watermark(self, layout.watermark)
            super().showPage()

    return WatermarkedCanvas


# --- the flowing parts --------------------------------------------------------

class _Rule(Table):
    """A full-width hairline, as a flowable."""

    def __init__(self, colour, thickness: float, width: float = CONTENT_WIDTH):
        super().__init__([['']], colWidths=[width], rowHeights=[thickness])
        self.setStyle(TableStyle([
            ('LINEABOVE', (0, 0), (-1, 0), thickness, colour),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))


def _pairs_table(fields: list[Field], styles: dict, width: float) -> Table:
    """Label on the right, value to its left — the card's inner grid."""
    rows = [f for f in fields if f.filled]
    if not rows:
        rows = [Field('', '')]
    widest = max(
        (pdfmetrics.stringWidth(str(f.label), FONT_BOLD, styles['label'].fontSize) for f in rows),
        default=0.0,
    )
    label_w = min(widest + LABEL_COL_GUTTER, width * LABEL_COL_MAX_SHARE)
    value_w = width - label_w
    data = [
        [para(f.value, styles['value'], value_w), para(f.label, styles['label'], label_w)]
        for f in rows
    ]
    table = Table(data, colWidths=[value_w, label_w])
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2.6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.6),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return table


def _details_card(layout: InvoiceLayout, styles: dict) -> Table:
    """One rounded card: the document and its customer beside the business."""
    business_w = CONTENT_WIDTH * BUSINESS_HALF
    document_w = CONTENT_WIDTH - business_w

    def column(heading: str, fields: list[Field], width: float) -> list:
        inner = width - 2 * CARD_PADDING
        return [
            para(heading, styles['heading'], inner),
            Spacer(1, 6.5),
            _pairs_table(fields, styles, inner),
        ]

    card = Table(
        [[column(layout.business_heading, layout.business_fields, business_w),
          column(layout.document_heading, layout.document_fields, document_w)]],
        colWidths=[business_w, document_w],
    )
    card.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CARD_BG),
        ('ROUNDEDCORNERS', [CARD_RADIUS] * 4),
        ('BOX', (0, 0), (-1, -1), 0.9, CARD_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
        ('LEFTPADDING', (0, 0), (-1, -1), CARD_PADDING),
        ('RIGHTPADDING', (0, 0), (-1, -1), CARD_PADDING),
    ]))
    return card


def _items_table(layout: InvoiceLayout, styles: dict) -> Table:
    widths = list(ITEM_COL_WIDTHS)
    # Absorb rounding into the description column so the table ends on CONTENT_RIGHT.
    widths[-1] += CONTENT_WIDTH - sum(widths)

    header = [
        para('סה"כ כולל מע"מ', styles['th'], widths[0]),
        para('מע"מ', styles['th'], widths[1]),
        para('סה"כ לפני מע"מ', styles['th'], widths[2]),
        # Broken where the sample breaks it, so the header row keeps its height
        # whatever the font measures.
        para('מחיר יחידה לפני\nמע"מ', styles['th'], widths[3]),
        para('כמות', styles['th'], widths[4]),
        para('תיאור פריט / שירות', styles['th_right'], widths[5] - 12),
    ]
    data = [header]
    for item in layout.items or [LineItem(description='—')]:
        description: list = [para(item.description or '—', styles['td'], widths[5] - 12)]
        if item.sub:
            description.append(para(item.sub, styles['td_sub'], widths[5] - 12))
        data.append([
            para(item.line_gross, styles['td_bold'], widths[0]),
            para(item.vat_rate, styles['td_num'], widths[1]),
            para(item.line_net, styles['td_num'], widths[2]),
            para(item.unit_price, styles['td_num'], widths[3]),
            para(item.quantity, styles['td_num'], widths[4]),
            description,
        ])

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('ROUNDEDCORNERS', [TABLE_RADIUS] * 4),
        ('BACKGROUND', (0, 0), (-1, 0), TABLE_HEAD_BG),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('LINEAFTER', (0, 0), (-2, 0), 0.6, TABLE_HEAD_SEP),
        ('LINEAFTER', (0, 1), (-2, -1), 0.6, ROW_SEP),
        ('LINEBELOW', (0, 1), (-1, -2), 0.6, ROW_SEP),
        # The header centres its one- and two-line cells against each other; a
        # body row hangs from the top, so a three-line description keeps its
        # numbers beside its first line.
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('VALIGN', (0, 1), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, 0), 9.5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 9.5),
        ('TOPPADDING', (0, 1), (-1, -1), 10.5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 10.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    return table


def _totals_card(layout: InvoiceLayout, styles: dict, width: float) -> Table:
    """
    The card at the foot: the VAT breakdown, a rule, then the amount due.

    The 14pt inset is the card's, not the cells' — splitting it across both
    columns would steal it from the label, which then wraps.
    """
    pad = 14.0
    inner = width - 2 * pad
    label_w = inner * 0.45
    value_w = inner - label_w

    def grid(rows: list[tuple[str, str]], label_style, value_style, valign: str) -> Table:
        table = Table(
            [[para(value, value_style, value_w), para(label, label_style, label_w)]
             for label, value in rows],
            colWidths=[value_w, label_w],
        )
        table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), valign),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))
        return table

    breakdown = [(row.label, row.value) for row in layout.totals if row.filled]
    body: list = []
    if breakdown:
        body += [grid(breakdown, styles['label'], styles['value'], 'TOP'), Spacer(1, 4.5)]
    body += [
        _Rule(CARD_BORDER, 0.8, inner),
        Spacer(1, 6),
        grid([(layout.grand_label, layout.grand_value)],
             styles['grand_label'], styles['grand_value'], 'MIDDLE'),
    ]

    card = Table([[body]], colWidths=[width])
    card.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CARD_BG),
        ('ROUNDEDCORNERS', [CARD_RADIUS] * 4),
        ('BOX', (0, 0), (-1, -1), 0.9, CARD_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 15),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 15),
        ('LEFTPADDING', (0, 0), (-1, -1), pad),
        ('RIGHTPADDING', (0, 0), (-1, -1), pad),
    ]))
    return card


def _payment_block(layout: InvoiceLayout, styles: dict, width: float) -> Table:
    body: list = [
        para(layout.payment_heading, styles['heading'], width),
        Spacer(1, 8),
        _pairs_table(layout.payment_fields, styles, width),
    ]
    if layout.payment_note:
        body += [Spacer(1, 4), para(layout.payment_note, styles['payment_note'], width)]
    block = Table([[body]], colWidths=[width])
    block.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    return block


def _bottom_row(layout: InvoiceLayout, styles: dict) -> Table:
    totals_w = 204.9
    gap = 24.4
    payment_w = CONTENT_WIDTH - totals_w - gap
    row = Table(
        [[_payment_block(layout, styles, payment_w), '',
          _totals_card(layout, styles, totals_w)]],
        colWidths=[payment_w, gap, totals_w],
    )
    row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return row


def _note_paragraph(note: Note, style: ParagraphStyle, width: float) -> Paragraph:
    """
    One line of small print: a bold lead-in, then the rest.

    The lead-in is the logical start of the line, so in visual order it lands at
    the right — last in the string. It keeps its own room on the first line and
    the body is wrapped around it, so a long note wraps under the lead-in rather
    than through it.
    """
    usable = max(width - 4, 10)
    lead = str(note.lead or '')
    lead_width = (
        pdfmetrics.stringWidth(lead, FONT_BOLD, style.fontSize) + 4 if lead else 0.0
    )
    lines = wrapped_lines(
        note.text, style.fontName, style.fontSize, usable,
        first_usable=max(usable - lead_width, 10),
    )
    out: list[str] = []
    for index, line in enumerate(lines):
        visual = escape(get_display(line)) if line else ''
        if index == 0 and lead:
            # An explicit face and colour, rather than <b>: it cannot depend on
            # a font family mapping being registered.
            tail = (
                f'<font name="{FONT_BOLD}" color="#{NOTE_STRONG.hexval()[2:]}">'
                f'{escape(get_display(lead))}</font>'
            )
            visual = f'{visual} {tail}' if visual else tail
        out.append(visual)
    return Paragraph('<br/>'.join(out), style)


def _signature_seal() -> SignatureSeal:
    return SignatureSeal(
        SEAL_DIAMETER,
        bold_font=FONT_BOLD,
        regular_font=FONT_REGULAR,
        ink=TITLE_COLOR,
        accent=MARK_YELLOW,
        fill=CARD_BG,
        bottom_text=ISSUER_NAME,
        company_number=ISSUER_COMPANY_NUMBER,
    )


def _notes_block(layout: InvoiceLayout, styles: dict) -> list:
    """The small print between a grey rule and a cyan one — with the seal at its left on a signed original."""
    lines = [n for n in layout.notes if (n.lead or n.text)]
    if not lines and not layout.signed_seal:
        return []
    width = CONTENT_WIDTH - SEAL_COLUMN if layout.signed_seal else CONTENT_WIDTH
    rows = [[_note_paragraph(note, styles['note'], width)] for note in lines] or [['']]
    block = Table(rows, colWidths=[width])
    no_padding = [
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]
    block.setStyle(TableStyle(no_padding))
    if layout.signed_seal:
        block = Table([[_signature_seal(), block]], colWidths=[SEAL_COLUMN, width])
        block.setStyle(TableStyle(no_padding + [('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    return [
        _Rule(RULE_GREY, 0.8), Spacer(1, 11),
        block,
        Spacer(1, 11), _Rule(RULE_CYAN, 0.7),
    ]


# --- the one entry point ------------------------------------------------------

def build_story(layout: InvoiceLayout) -> list:
    styles = _styles()
    story: list = [
        para(layout.title, styles['title'], CONTENT_WIDTH),
        Spacer(1, 7),
        para(layout.copy_mark, styles['copy_mark'], CONTENT_WIDTH),
        Spacer(1, 20),
        _details_card(layout, styles),
        Spacer(1, 22),
        para(layout.items_heading, styles['heading'], CONTENT_WIDTH),
        Spacer(1, 9),
        _items_table(layout, styles),
        Spacer(1, 17),
        _bottom_row(layout, styles),
    ]
    notes = _notes_block(layout, styles)
    if notes:
        story.append(Spacer(1, 26))
        # The small print belongs with the document, never alone on a last page.
        story.append(KeepTogether(notes))
    return story


def render_invoice_pdf(layout: InvoiceLayout) -> bytes:
    """Draw `layout` and return the PDF bytes."""
    ensure_fonts_registered()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=PAGE_WIDTH - CONTENT_RIGHT,
        topMargin=BODY_TOP,
        bottomMargin=PAGE_HEIGHT - BODY_BOTTOM,
        title=layout.pdf_title or layout.title,
        author=layout.pdf_author or '',
    )
    on_page = _page_painter(layout)
    doc.build(
        build_story(layout),
        onFirstPage=on_page,
        onLaterPages=on_page,
        canvasmaker=_canvas_maker(layout),
    )
    return buffer.getvalue()
