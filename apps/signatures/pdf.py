"""
A stored signature as a PDF the office can download and hand over.

Hebrew is done the way apps.store.invoice_pdf and
apps.documents.period_report_pdf do it: Heebo embedded as a TTF, and
python-bidi reordering text into visual order, because reportlab draws glyphs
in the order given and does not implement the bidi algorithm. The terms are
long paragraphs, so each one is measured and wrapped first and every resulting
line is reordered on its own — reordering a whole paragraph and letting
Paragraph wrap it would break lines in the wrong place and scramble the text.
"""
from __future__ import annotations

import io
import re
from urllib.parse import quote
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from apps.signatures.text import html_to_paragraphs
from apps.store.invoice_pdf import (
    BORDER,
    BRAND_NAVY,
    BRAND_PURPLE,
    PANEL_BG,
    _ensure_fonts_registered,
    _rtl,
)

PAGE_WIDTH, PAGE_HEIGHT = A4
SIDE_MARGIN = 2.0 * cm
# SimpleDocTemplate's frame pads its content by 6pt on each side, so the width
# a flowable really gets is the margins' width minus that. Measuring against
# the margins alone let lines that fit on paper in theory overflow the frame,
# and Paragraph re-wrapped them.
FRAME_PADDING = 6
CONTENT_WIDTH = PAGE_WIDTH - 2 * SIDE_MARGIN - 2 * FRAME_PADDING
LABEL_WIDTH = 4.4 * cm
VALUE_WIDTH = CONTENT_WIDTH - LABEL_WIDTH
CELL_PADDING = 5
SIGNATURE_MAX_WIDTH = 8.0 * cm
SIGNATURE_MAX_HEIGHT = 3.5 * cm

CONSENT_LABELS = [
    ('health', 'הצהרת בריאות'),
    ('terms', 'אישור התקנון והנהלים'),
    ('computerized_documents', 'הסכמה לקבלת מסמכים ממוחשבים'),
]


def _styles() -> dict[str, ParagraphStyle]:
    return {
        'title': ParagraphStyle(
            'SigTitle', fontName='Heebo-Bold', fontSize=19,
            textColor=BRAND_PURPLE, alignment=TA_CENTER, leading=24,
        ),
        'subtitle': ParagraphStyle(
            'SigSubtitle', fontName='Heebo', fontSize=11,
            textColor=BRAND_NAVY, alignment=TA_CENTER, leading=15,
        ),
        'heading': ParagraphStyle(
            'SigHeading', fontName='Heebo-Bold', fontSize=12,
            textColor=BRAND_PURPLE, alignment=TA_RIGHT, leading=16,
        ),
        'label': ParagraphStyle(
            'SigLabel', fontName='Heebo-Bold', fontSize=9.5,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=13,
        ),
        'value': ParagraphStyle(
            'SigValue', fontName='Heebo', fontSize=9.5,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=13,
        ),
        'body': ParagraphStyle(
            'SigBody', fontName='Heebo', fontSize=9.5,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=14,
        ),
        'small_label': ParagraphStyle(
            'SigSmallLabel', fontName='Heebo', fontSize=7,
            textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=9,
        ),
        # Hashes, the IP and the user agent are left-to-right text.
        'small_value': ParagraphStyle(
            'SigSmallValue', fontName='Heebo', fontSize=7,
            textColor=BRAND_NAVY, alignment=TA_LEFT, leading=9,
        ),
    }


def _wrapped(text, style: ParagraphStyle, width: float) -> Paragraph:
    """
    Wrap to `width` first, then bidi-reorder each line on its own.

    The technique of period_report_pdf._rtl_cell, plus escaping each line:
    the signed text is free text and may contain '&' or '<', which Paragraph
    would otherwise read as markup.

    Lines are measured a few points short of the width. Paragraph re-wraps
    any line that comes out even slightly wider, and in visual order the
    logical first word sits at the end of the string — so a paragraph's
    number ("2.") would be the word pushed onto a line of its own.
    """
    usable = max(width - 6, 10)
    lines: list[str] = []
    for source_line in str(text or '').splitlines() or ['']:
        current: list[str] = []
        for word in source_line.split(' '):
            candidate = ' '.join(current + [word])
            if not current or pdfmetrics.stringWidth(candidate, style.fontName, style.fontSize) <= usable:
                current.append(word)
            else:
                lines.append(' '.join(current))
                current = [word]
        lines.append(' '.join(current))
    return Paragraph('<br/>'.join(escape(_rtl(line)) for line in lines), style)


def _local(dt, fmt: str) -> str:
    return timezone.localtime(dt).strftime(fmt) if dt else ''


def _label_table(rows, label_style, value_style, *, label_width=LABEL_WIDTH, shaded=True) -> Table:
    """Label on the right, value on its left — the reading order of a Hebrew form."""
    value_width = CONTENT_WIDTH - label_width
    data = [
        [
            _wrapped(value, value_style, value_width - 2 * CELL_PADDING),
            _wrapped(label, label_style, label_width - 2 * CELL_PADDING),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[value_width, label_width])
    commands = [
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), CELL_PADDING),
        ('RIGHTPADDING', (0, 0), (-1, -1), CELL_PADDING),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]
    if shaded:
        commands += [
            ('BACKGROUND', (1, 0), (1, -1), PANEL_BG),
            ('BOX', (0, 0), (-1, -1), 0.6, BORDER),
            ('INNERGRID', (0, 0), (-1, -1), 0.4, BORDER),
        ]
    table.setStyle(TableStyle(commands))
    return table


def _signature_flowable(png: bytes):
    """The signature image scaled into its box, or None if it cannot be drawn."""
    try:
        width_px, height_px = ImageReader(io.BytesIO(png)).getSize()
        scale = min(SIGNATURE_MAX_WIDTH / width_px, SIGNATURE_MAX_HEIGHT / height_px)
        image = Image(io.BytesIO(png), width=width_px * scale, height=height_px * scale)
    except Exception:
        return None
    box = Table([[image]], colWidths=[SIGNATURE_MAX_WIDTH + 0.6 * cm])
    box.hAlign = 'RIGHT'
    box.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.6, BORDER),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    return box


def generate_signature_pdf(signature) -> bytes:
    _ensure_fonts_registered()
    styles = _styles()
    buffer = io.BytesIO()
    title = signature.document_title or signature.get_kind_display()
    signed_at = _local(signature.signed_at, '%d/%m/%Y %H:%M')

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Heebo', 7.5)
        canvas.setFillColor(BRAND_NAVY)
        canvas.drawCentredString(
            PAGE_WIDTH / 2, 1.1 * cm,
            _rtl(f'{title} · {signature.signer_name} · {signed_at} · עמוד {doc.page}'),
        )
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=SIDE_MARGIN,
        rightMargin=SIDE_MARGIN,
        topMargin=1.8 * cm,
        bottomMargin=1.9 * cm,
        title=title,
        author='Kogo',
    )

    story = [
        Paragraph(escape(_rtl(title)), styles['title']),
        Paragraph(escape(_rtl(f'מסמך חתום · {signature.get_kind_display()}')), styles['subtitle']),
        Spacer(1, 0.5 * cm),
        Paragraph(escape(_rtl('פרטי החתימה')), styles['heading']),
        Spacer(1, 0.15 * cm),
    ]

    children = ', '.join(child.full_name for child in signature.children.all())
    details = [
        ('שם החותם', signature.signer_name or '—'),
        ('ת.ז.', signature.signer_id_number or '—'),
        ('טלפון', signature.signer_phone or '—'),
    ]
    if signature.signer_email:
        details.append(('אימייל', signature.signer_email))
    details.append(('מועד החתימה', signed_at))
    if signature.business_customer_id:
        details.append(('לקוח עסקי', signature.business_customer.full_name))
    if children:
        details.append(('עבור', children))
    if signature.branch_id:
        details.append(('סניף', signature.branch.name))
    story.append(_label_table(details, styles['label'], styles['value']))

    consents = signature.consents or {}
    story += [
        Spacer(1, 0.45 * cm),
        Paragraph(escape(_rtl('הצהרות ואישורים')), styles['heading']),
        Spacer(1, 0.15 * cm),
        _label_table(
            # Only what the signed document asked: a rental contract has no health
            # declaration, and "לא אושר" there would misstate what was signed.
            [(label, 'אושר' if consents.get(key) else 'לא אושר') for key, label in CONSENT_LABELS if key in consents],
            styles['label'], styles['value'], label_width=CONTENT_WIDTH - 3.0 * cm,
        ),
        Spacer(1, 0.45 * cm),
        Paragraph(escape(_rtl('נוסח המסמך כפי שנחתם')), styles['heading']),
        Spacer(1, 0.15 * cm),
    ]

    for paragraph in html_to_paragraphs(signature.document_html) or ['—']:
        story.append(_wrapped(paragraph, styles['body'], CONTENT_WIDTH))
        story.append(Spacer(1, 0.12 * cm))

    signature_block = [
        Spacer(1, 0.4 * cm),
        Paragraph(escape(_rtl('חתימה')), styles['heading']),
        Spacer(1, 0.15 * cm),
    ]
    image = _signature_flowable(bytes(signature.signature_png or b''))
    if image is not None:
        signature_block.append(image)
    else:
        signature_block.append(_wrapped('לא ניתן להציג את תמונת החתימה', styles['value'], CONTENT_WIDTH))
    signature_block.append(Spacer(1, 0.1 * cm))
    signature_block.append(_wrapped(f'נחתם ב־{signed_at}', styles['value'], CONTENT_WIDTH))
    story.append(KeepTogether(signature_block))

    story += [
        Spacer(1, 0.5 * cm),
        _label_table(
            [
                ('מזהה החתימה', str(signature.id)),
                ('גיבוב נוסח המסמך (SHA-256)', signature.document_sha256 or '—'),
                ('גיבוב תמונת החתימה (SHA-256)', signature.signature_sha256 or '—'),
                ('כתובת IP', signature.ip_address or '—'),
                ('דפדפן', signature.user_agent or '—'),
            ],
            styles['small_label'], styles['small_value'], label_width=4.6 * cm, shaded=False,
        ),
    ]

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()


def signature_pdf_filename(signature) -> str:
    day = _local(signature.signed_at, '%Y-%m-%d')
    signer = re.sub(r'[\\/:*?"<>|\s]+', '-', signature.signer_name or '').strip('-.') or 'signer'
    return f'signature-{day}-{signer}.pdf'


def signature_pdf_content_disposition(signature) -> str:
    """attachment; the Hebrew name in filename*, with an ASCII filename for old clients."""
    filename = signature_pdf_filename(signature)
    try:
        filename.encode('ascii')
    except UnicodeEncodeError:
        fallback = f'signature-{_local(signature.signed_at, "%Y-%m-%d")}.pdf'
        return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    return f'attachment; filename="{filename}"'
