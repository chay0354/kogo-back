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

One page, always (the owner's rule). A document is drawn the sample's way
first, and when that is one page it is the result — an ordinary document is
never touched. Only one that runs over is drawn again, pressed to fit: the
white space closes first (every letter keeps its size), and if that is not
enough the body under the document's name is scaled down evenly. See
:class:`Fit` and :func:`render_invoice_pdf`.

Hebrew RTL: text is wrapped to the column width *first* and each resulting line
is bidi-reordered on its own (the technique of ``period_report_pdf._rtl_cell``
and ``signatures.pdf._wrapped``). Reordering a whole string and letting
Paragraph wrap the visually-ordered result breaks lines at the wrong points and
scrambles a long Hebrew name.
"""
from __future__ import annotations

import io
import logging
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
    Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.platypus.doctemplate import LayoutError

from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_NAME
from apps.documents.signature_seal import SignatureSeal

logger = logging.getLogger(__name__)

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
    # The round seal beside the small print — on a signed, stored file only.
    signed_seal: bool = False
    # What the seal says in its centre; '' is "מסמך ממוחשב". The archive copy of
    # a document issued before signing says "העתק לארכיון" there.
    seal_centre_text: str = ''
    pdf_title: str = ''
    pdf_author: str = ''


# --- how hard a document is pressed onto its one page -------------------------------

@dataclass(frozen=True)
class Fit:
    """
    How far a document's body is pressed to stay on its one page.

    ``tight`` closes the white space — 0 is the sample's own spacing, 1 the
    tightest that still reads as the same design — and leaves every letter its
    size. ``scale`` then shrinks the body evenly: it is laid out ``1/scale``
    times wider and drawn at ``scale``, so the cards and the table still span
    the page while everything inside them (type, padding, corners, the seal)
    is that much smaller.

    The default presses nothing: every number below comes back exactly as it
    was written, which is what keeps an ordinary document byte-for-byte the
    sample's.
    """
    tight: float = 0.0
    scale: float = 1.0
    # The payment details under the totals (True), beside them (False), or
    # decided by their height as an ordinary document's are (None).
    stacked: bool | None = None

    def gap(self, normal: float, tightest: float) -> float:
        """A piece of white space: `normal` in the sample, `tightest` when fully pressed."""
        return normal - self.tight * (normal - tightest)

    def wide(self, points: float) -> float:
        """A width of the page, in the units the body is laid out in."""
        return points / self.scale


NATURAL = Fit()


# --- styles -------------------------------------------------------------------

def _styles(fit: Fit = NATURAL) -> dict[str, ParagraphStyle]:
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
            'InvLabel', fontName=FONT_BOLD, fontSize=8.25, leading=fit.gap(13, 10.5),
            textColor=LABEL_COLOR, alignment=TA_RIGHT,
        ),
        'value': ParagraphStyle(
            'InvValue', fontName=FONT_REGULAR, fontSize=8.25, leading=fit.gap(13, 10.5),
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
            'InvTd', fontName=FONT_REGULAR, fontSize=7.9, leading=fit.gap(11, 10),
            textColor=BODY_TEXT, alignment=TA_RIGHT,
        ),
        'td_num': ParagraphStyle(
            'InvTdNum', fontName=FONT_REGULAR, fontSize=7.9, leading=fit.gap(11, 10),
            textColor=BODY_TEXT, alignment=TA_CENTER,
        ),
        'td_bold': ParagraphStyle(
            'InvTdBold', fontName=FONT_BOLD, fontSize=7.9, leading=fit.gap(11, 10),
            textColor=BODY_TEXT, alignment=TA_CENTER,
        ),
        'td_sub': ParagraphStyle(
            'InvTdSub', fontName=FONT_REGULAR, fontSize=7.1, leading=fit.gap(10, 9),
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
            'InvPaymentNote', fontName=FONT_REGULAR, fontSize=7.1, leading=fit.gap(10, 9),
            textColor=ACCENT_NOTE, alignment=TA_RIGHT,
        ),
        'note': ParagraphStyle(
            'InvNote', fontName=FONT_REGULAR, fontSize=7.1, leading=fit.gap(12.1, 9.6),
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
    # A document is one page (render_invoice_pdf), so this is reached only if
    # pressing it onto the page failed and the flowing drawing went out instead.
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


def _pairs_table(fields: list[Field], styles: dict, width: float, fit: Fit = NATURAL) -> Table:
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
        ('TOPPADDING', (0, 0), (-1, -1), fit.gap(2.6, 0.75)),
        ('BOTTOMPADDING', (0, 0), (-1, -1), fit.gap(2.6, 0.75)),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return table


def _details_card(layout: InvoiceLayout, styles: dict, fit: Fit = NATURAL) -> Table:
    """One rounded card: the document and its customer beside the business."""
    content_w = fit.wide(CONTENT_WIDTH)
    business_w = content_w * BUSINESS_HALF
    document_w = content_w - business_w

    def column(heading: str, fields: list[Field], width: float) -> list:
        inner = width - 2 * CARD_PADDING
        return [
            para(heading, styles['heading'], inner),
            Spacer(1, fit.gap(6.5, 4)),
            _pairs_table(fields, styles, inner, fit),
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
        ('TOPPADDING', (0, 0), (-1, -1), fit.gap(14, 8)),
        ('BOTTOMPADDING', (0, 0), (-1, -1), fit.gap(16, 9)),
        ('LEFTPADDING', (0, 0), (-1, -1), CARD_PADDING),
        ('RIGHTPADDING', (0, 0), (-1, -1), CARD_PADDING),
    ]))
    return card


def _items_table(layout: InvoiceLayout, styles: dict, fit: Fit = NATURAL) -> Table:
    widths = [fit.wide(width) for width in ITEM_COL_WIDTHS]
    # Absorb rounding into the description column so the table ends on CONTENT_RIGHT.
    widths[-1] += fit.wide(CONTENT_WIDTH) - sum(widths)

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
        ('TOPPADDING', (0, 0), (-1, 0), fit.gap(9.5, 4.5)),
        ('BOTTOMPADDING', (0, 0), (-1, 0), fit.gap(9.5, 4.5)),
        ('TOPPADDING', (0, 1), (-1, -1), fit.gap(10.5, 3)),
        ('BOTTOMPADDING', (0, 1), (-1, -1), fit.gap(10.5, 3)),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    return table


def _totals_card(layout: InvoiceLayout, styles: dict, width: float, fit: Fit = NATURAL) -> Table:
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
            ('TOPPADDING', (0, 0), (-1, -1), fit.gap(2, 0.75)),
            ('BOTTOMPADDING', (0, 0), (-1, -1), fit.gap(2, 0.75)),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))
        return table

    breakdown = [(row.label, row.value) for row in layout.totals if row.filled]
    body: list = []
    if breakdown:
        body += [grid(breakdown, styles['label'], styles['value'], 'TOP'), Spacer(1, fit.gap(4.5, 3))]
    body += [
        _Rule(CARD_BORDER, 0.8, inner),
        Spacer(1, fit.gap(6, 3)),
        grid([(layout.grand_label, layout.grand_value)],
             styles['grand_label'], styles['grand_value'], 'MIDDLE'),
    ]

    card = Table([[body]], colWidths=[width])
    card.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CARD_BG),
        ('ROUNDEDCORNERS', [CARD_RADIUS] * 4),
        ('BOX', (0, 0), (-1, -1), 0.9, CARD_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), fit.gap(15, 8)),
        ('BOTTOMPADDING', (0, 0), (-1, -1), fit.gap(15, 8)),
        ('LEFTPADDING', (0, 0), (-1, -1), pad),
        ('RIGHTPADDING', (0, 0), (-1, -1), pad),
    ]))
    return card


def _payment_block(layout: InvoiceLayout, styles: dict, width: float, fit: Fit = NATURAL) -> Table:
    body: list = [
        para(layout.payment_heading, styles['heading'], width),
        Spacer(1, fit.gap(8, 4)),
        _pairs_table(layout.payment_fields, styles, width, fit),
    ]
    if layout.payment_note:
        body += [Spacer(1, fit.gap(4, 2.5)), para(layout.payment_note, styles['payment_note'], width)]
    block = Table([[body]], colWidths=[width])
    block.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), fit.gap(10, 4)),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    return block


TOTALS_WIDTH = 204.9
TOTALS_GAP = 24.4
# The payment details sit beside the totals in one table row, and a table row
# cannot break across pages: a row taller than the page is a LayoutError, and
# the document does not render at all. A receipt for a check plan — a dozen
# checks, three rows each — is that tall. Past this height the payment details
# go under the totals instead, as rows that break across pages.
BOTTOM_ROW_MAX_HEIGHT = (BODY_BOTTOM - BODY_TOP) * 0.6


def _bottom_row(layout: InvoiceLayout, styles: dict, fit: Fit = NATURAL, *, with_payment: bool = True) -> Table:
    totals_w = fit.wide(TOTALS_WIDTH)
    gap_w = fit.wide(TOTALS_GAP)
    payment_w = fit.wide(CONTENT_WIDTH) - totals_w - gap_w
    row = Table(
        [[_payment_block(layout, styles, payment_w, fit) if with_payment else '', '',
          _totals_card(layout, styles, totals_w, fit)]],
        colWidths=[payment_w, gap_w, totals_w],
    )
    row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return row


def _bottom(layout: InvoiceLayout, styles: dict, fit: Fit = NATURAL) -> list:
    """
    The payment details beside the totals — the sample's layout, and every
    ordinary document's. When they run taller than BOTTOM_ROW_MAX_HEIGHT, the
    totals come first and the payment details follow at full width, their rows
    free to continue on the next page.

    A document being pressed onto its page has already been told which of the
    two it is (``fit.stacked``), so the choice does not move under it while the
    pressing is worked out.
    """
    content_w = fit.wide(CONTENT_WIDTH)
    stacked = fit.stacked
    if not stacked:
        row = _bottom_row(layout, styles, fit)
        if stacked is None:
            _, height = row.wrap(content_w, PAGE_HEIGHT)
            stacked = height > BOTTOM_ROW_MAX_HEIGHT
        if not stacked:
            return [row]
    tall: list = [
        _bottom_row(layout, styles, fit, with_payment=False),
        Spacer(1, fit.gap(17, 9)),
        para(layout.payment_heading, styles['heading'], content_w),
        Spacer(1, fit.gap(8, 4)),
        _pairs_table(layout.payment_fields, styles, content_w, fit),
    ]
    if layout.payment_note:
        tall += [Spacer(1, fit.gap(4, 2.5)), para(layout.payment_note, styles['payment_note'], content_w)]
    return tall


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


def _signature_seal(centre_text: str = '') -> SignatureSeal:
    return SignatureSeal(
        SEAL_DIAMETER,
        bold_font=FONT_BOLD,
        regular_font=FONT_REGULAR,
        ink=TITLE_COLOR,
        accent=MARK_YELLOW,
        fill=CARD_BG,
        bottom_text=ISSUER_NAME,
        company_number=ISSUER_COMPANY_NUMBER,
        centre_text=centre_text,
    )


def _notes_block(layout: InvoiceLayout, styles: dict, fit: Fit = NATURAL) -> list:
    """The small print between a grey rule and a cyan one — with the seal at its left on a signed original."""
    lines = [n for n in layout.notes if (n.lead or n.text)]
    if not lines and not layout.signed_seal:
        return []
    content_w = fit.wide(CONTENT_WIDTH)
    width = content_w - SEAL_COLUMN if layout.signed_seal else content_w
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
        block = Table([[_signature_seal(layout.seal_centre_text), block]], colWidths=[SEAL_COLUMN, width])
        block.setStyle(TableStyle(no_padding + [('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    return [
        _Rule(RULE_GREY, 0.8, content_w), Spacer(1, fit.gap(11, 6)),
        block,
        Spacer(1, fit.gap(11, 6)), _Rule(RULE_CYAN, 0.7, content_w),
    ]


# --- the one entry point ------------------------------------------------------

def _head(layout: InvoiceLayout, styles: dict) -> list:
    """The document's name and its מקור / העתק mark — never pressed, the same on every document."""
    return [
        para(layout.title, styles['title'], CONTENT_WIDTH),
        Spacer(1, 7),
        para(layout.copy_mark, styles['copy_mark'], CONTENT_WIDTH),
    ]


def _body(layout: InvoiceLayout, styles: dict, fit: Fit = NATURAL, *, flowing: bool = True) -> list:
    """
    Everything under the document's name.

    `flowing` is the ordinary drawing, where the story may break across pages
    and the small print is kept together; a pressed body is one block that
    never breaks, so it takes the small print as plain rows.
    """
    body: list = [
        Spacer(1, fit.gap(20, 10)),
        _details_card(layout, styles, fit),
        Spacer(1, fit.gap(22, 10)),
        para(layout.items_heading, styles['heading'], fit.wide(CONTENT_WIDTH)),
        Spacer(1, fit.gap(9, 5)),
        _items_table(layout, styles, fit),
        Spacer(1, fit.gap(17, 9)),
        *_bottom(layout, styles, fit),
    ]
    notes = _notes_block(layout, styles, fit)
    if notes:
        body.append(Spacer(1, fit.gap(26, 10)))
        # The small print belongs with the document, never alone on a last page.
        body += [KeepTogether(notes)] if flowing else notes
    return body


def build_story(layout: InvoiceLayout) -> list:
    """The sample's own drawing: what every document that fits its page is drawn from."""
    styles = _styles()
    return _head(layout, styles) + _body(layout, styles)


# --- one page, always ---------------------------------------------------------

# Kept clear at the foot of the pressed body, so rounding never tips it over.
FIT_SLACK = 1.0
# Below this the table's type is under 5pt: the document is still one page, as
# the owner asked, but it is small enough to be worth a line in the log.
SMALL_PRINT_SCALE = 0.64


def _measure(layout: InvoiceLayout, fit: Fit, avail_width: float, canvas=None) -> tuple[list, float]:
    """The body pressed by `fit`: each flowable with its size, and their total height (layout units)."""
    layout_width = fit.wide(avail_width)
    parts: list = []
    total = 0.0
    for flowable in _body(layout, _styles(fit), fit, flowing=False):
        width, height = flowable.wrapOn(canvas, layout_width, PAGE_HEIGHT * 1000)
        parts.append((flowable, width, height))
        total += height
    return parts, total


def _stacks_shorter(layout: InvoiceLayout, avail_width: float) -> bool:
    """
    Whether the payment details take less of the page under the totals than beside them.

    Beside is the sample's layout and stays unless under is really shorter —
    a dozen checks, whose details wrap in the narrow column, is the case.
    """
    def height(stacked: bool) -> float:
        fit = Fit(tight=1.0, stacked=stacked)
        return sum(
            flowable.wrap(fit.wide(avail_width), PAGE_HEIGHT * 1000)[1]
            for flowable in _bottom(layout, _styles(fit), fit)
        )

    return height(True) < height(False)


def _press(layout: InvoiceLayout, avail_width: float, room: float, canvas=None) -> tuple[Fit, list, float]:
    """
    The least pressing that brings the body inside `room` points of height.

    White space first: the height falls in a straight line as it closes, so the
    least tightening that fits is worked out, not searched for. Only when the
    tightest spacing is still too tall is the body scaled, and then by no more
    than it needs.
    """
    stacked = _stacks_shorter(layout, avail_width)

    def measured(tight: float, scale: float) -> tuple[Fit, list, float]:
        fit = Fit(tight=tight, scale=scale, stacked=stacked)
        parts, height = _measure(layout, fit, avail_width, canvas)
        return fit, parts, height

    natural = measured(0.0, 1.0)
    if natural[2] <= room:
        return natural
    tightest = measured(1.0, 1.0)
    if tightest[2] <= room:
        need = (natural[2] - room) / (natural[2] - tightest[2])
        chosen = measured(min(1.0, need + 0.002), 1.0)
        return chosen if chosen[2] <= room else tightest

    # Laid out wider, long text wraps less, so the height at a scale is not the
    # height at full size: start from full size's and correct until it fits.
    scale = room / tightest[2]
    best = None
    for _ in range(8):
        candidate = measured(1.0, scale)
        used = candidate[2] * scale
        if used <= room:
            best = candidate
            break
        scale *= room / used * 0.995
    if best is None:
        return candidate          # the drawing itself still holds it to the room (_PressedBody.wrap)
    # ...and when the wrapping gave room back, take it: the largest scale that fits.
    low, high = best[0].scale, 1.0
    if best[2] * low < room * 0.985:
        for _ in range(6):
            middle = (low + high) / 2
            candidate = measured(1.0, middle)
            if candidate[2] * middle <= room:
                best, low = candidate, middle
            else:
                high = middle
    return best


class _PressedBody(Flowable):
    """
    The body of a document that would run over, as one block that fits what is
    left of its page under the document's name.

    The pressing is worked out when the frame says how much room there is, so
    it is exact whatever the title above it took.
    """

    def __init__(self, layout: InvoiceLayout):
        super().__init__()
        self.layout = layout
        self.fit = NATURAL
        self.draw_scale = 1.0
        self._parts: list = []

    def wrap(self, avail_width, avail_height):
        room = max(avail_height - FIT_SLACK, 1.0)
        self.fit, self._parts, height = _press(
            self.layout, avail_width, room, getattr(self, 'canv', None),
        )
        # The last word: whatever was worked out, the block is never taller than the room.
        self.draw_scale = min(self.fit.scale, room / height) if height > 0 else self.fit.scale
        self.width = avail_width
        self.height = height * self.draw_scale
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        layout_width = self.fit.wide(self.width)
        canvas.saveState()
        # Centred only if the last word above had to shrink it past its layout.
        canvas.translate((self.width - layout_width * self.draw_scale) / 2, self.height)
        canvas.scale(self.draw_scale, self.draw_scale)
        y = 0.0
        for flowable, width, height in self._parts:
            y -= height
            # As a frame places it: a table centres on the column, text starts at its edge.
            flowable.drawOn(canvas, 0, y, _sW=layout_width - width)
        canvas.restoreState()


def build_pressed_story(layout: InvoiceLayout) -> tuple[list, _PressedBody]:
    """The same document with its body pressed onto the one page."""
    body = _PressedBody(layout)
    return _head(layout, _styles()) + [body], body


def _draw(layout: InvoiceLayout, story: list) -> tuple[bytes, int]:
    """`story` on the design's page: the PDF bytes and how many pages it took."""
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
        story,
        onFirstPage=on_page,
        onLaterPages=on_page,
        canvasmaker=_canvas_maker(layout),
    )
    return buffer.getvalue(), doc.page


def render_invoice_pdf(layout: InvoiceLayout) -> bytes:
    """
    Draw `layout` and return the PDF bytes — one A4 page, whatever it holds.

    The sample's drawing comes first, and when it is one page it is what goes
    out: a document that fits is not measured, pressed or changed in any way.
    Only one that runs over (or holds a block taller than a page, which the
    flowing drawing cannot place at all) is drawn again with its body pressed.

    Pressing must never cost a document: if it fails, the flowing drawing goes
    out as it always did and the failure is logged.
    """
    ensure_fonts_registered()
    try:
        flowing, pages = _draw(layout, build_story(layout))
    except LayoutError:
        flowing, pages = b'', 0
    if pages == 1:
        return flowing

    name = layout.pdf_title or layout.title
    try:
        story, body = build_pressed_story(layout)
        pressed, pressed_pages = _draw(layout, story)
    except Exception:
        if not flowing:
            raise
        logger.exception('Document %s could not be pressed onto one page; drawn on %s pages', name, pages)
        return flowing
    if pressed_pages != 1:
        logger.error('Document %s still took %s pages after pressing', name, pressed_pages)
    elif body.draw_scale < SMALL_PRINT_SCALE:
        logger.warning('Document %s fits one page only at %.0f%% size', name, body.draw_scale * 100)
    return pressed
