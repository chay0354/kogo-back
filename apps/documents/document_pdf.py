"""
A formal document rendered locally.

Tranzila's PDF stays the official copy whenever one exists. This renderer is
for the documents that never get one — drafts, credit invoices, and anything
Tranzila failed to issue — and it prints exactly what the record stores. No
amount is derived here: every figure is a stored column, so the page always
reconciles with the row it came from.
"""
from __future__ import annotations

import io
import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.documents.issuer import (
    ISSUER_ADDRESS, ISSUER_COMPANY_NUMBER, ISSUER_EMAIL, ISSUER_NAME, ISSUER_PHONE,
)
from apps.documents.models import DOCUMENT_TYPE_CHOICES, FormalDocument
from apps.store.invoice_pdf import (
    BORDER, BRAND_NAVY, BRAND_ORANGE, BRAND_PURPLE, PANEL_BG, _ensure_fonts_registered, _money, _rtl,
)

PAGE_WIDTH, PAGE_HEIGHT = A4
SIDE_MARGIN = 1.6 * cm
TOP_MARGIN = 1.2 * cm
BOTTOM_MARGIN = 2.2 * cm
CONTENT_WIDTH = PAGE_WIDTH - 2 * SIDE_MARGIN
RADIUS = 8

TYPE_LABELS = dict(DOCUMENT_TYPE_CHOICES)
# Allocation numbers are mandatory on tax invoices above this net amount
# (Israel Tax Authority, from 1 June 2026). The document says whether one is
# needed; it cannot invent one.
ALLOCATION_THRESHOLD = Decimal('5000')
TAX_DOCUMENT_TYPES = ('tax_invoice', 'combined', 'credit_invoice')

_LOGO_IMAGE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'store', 'assets', 'letterhead', 'image2.png'
)
LOGO_RATIO = 524 / 656

PAYMENT_METHOD_LABELS = {
    'cash': 'מזומן', 'credit_card': 'כרטיס אשראי', 'card': 'כרטיס אשראי',
    'bank_transfer': 'העברה בנקאית', 'check': "צ'ק", 'bit': 'ביט', 'other': 'אחר',
}


def _styles() -> dict:
    return {
        'title': ParagraphStyle('DocTitle', fontName='Heebo-Bold', fontSize=19, leading=24,
                                textColor=BRAND_NAVY, alignment=TA_CENTER),
        'origin': ParagraphStyle('DocOrigin', fontName='Heebo-Bold', fontSize=10, leading=13,
                                 textColor=BRAND_NAVY, alignment=TA_CENTER),
        'section': ParagraphStyle('DocSection', fontName='Heebo-Bold', fontSize=11, leading=14,
                                  textColor=BRAND_NAVY, alignment=TA_RIGHT),
        'label': ParagraphStyle('DocLabel', fontName='Heebo-Bold', fontSize=8.5, leading=12,
                                textColor=BRAND_NAVY, alignment=TA_RIGHT),
        'value': ParagraphStyle('DocValue', fontName='Heebo', fontSize=8.5, leading=12,
                                textColor=colors.HexColor('#2a2d4a'), alignment=TA_LEFT),
        'th': ParagraphStyle('DocTh', fontName='Heebo-Bold', fontSize=8.5, leading=11,
                             textColor=colors.white, alignment=TA_CENTER),
        'td': ParagraphStyle('DocTd', fontName='Heebo', fontSize=8.5, leading=11,
                             textColor=colors.HexColor('#2a2d4a'), alignment=TA_RIGHT),
        'td_num': ParagraphStyle('DocTdNum', fontName='Heebo', fontSize=8.5, leading=11,
                                 textColor=colors.HexColor('#2a2d4a'), alignment=TA_CENTER),
        'td_bold': ParagraphStyle('DocTdBold', fontName='Heebo-Bold', fontSize=8.5, leading=11,
                                  textColor=BRAND_NAVY, alignment=TA_CENTER),
        'sub': ParagraphStyle('DocSub', fontName='Heebo', fontSize=7.5, leading=10,
                              textColor=colors.HexColor('#6b6f8a'), alignment=TA_RIGHT),
        'grand_label': ParagraphStyle('DocGrandL', fontName='Heebo-Bold', fontSize=11, leading=14,
                                      textColor=BRAND_NAVY, alignment=TA_RIGHT),
        'grand_value': ParagraphStyle('DocGrandV', fontName='Heebo-Bold', fontSize=15, leading=18,
                                      textColor=BRAND_NAVY, alignment=TA_LEFT),
        'note': ParagraphStyle('DocNote', fontName='Heebo', fontSize=7.5, leading=11,
                               textColor=colors.HexColor('#4a4e6a'), alignment=TA_RIGHT),
        'credit': ParagraphStyle('DocCredit', fontName='Heebo-Bold', fontSize=8.5, leading=12,
                                 textColor=BRAND_ORANGE, alignment=TA_RIGHT),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_rtl(text or ''), style)


def _card(extra=None, padding=(5, 5, 8, 8)) -> TableStyle:
    top, bottom, left, right = padding
    base = [
        ('ROUNDEDCORNERS', [RADIUS] * 4),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), top),
        ('BOTTOMPADDING', (0, 0), (-1, -1), bottom),
        ('LEFTPADDING', (0, 0), (-1, -1), left),
        ('RIGHTPADDING', (0, 0), (-1, -1), right),
    ]
    return TableStyle(base + list(extra or []))


# --- who the document is for ------------------------------------------------

def _customer_details(doc: FormalDocument) -> list[tuple[str, str]]:
    rows = []
    if doc.business_customer_id and doc.business_customer:
        bc = doc.business_customer
        rows.append(('שם הלקוח', bc.full_name or ''))
        for attr, label in (('company_number', 'ח.פ. / ע.מ.'), ('phone', 'טלפון'), ('email', 'אימייל'), ('address', 'כתובת')):
            value = getattr(bc, attr, '') or ''
            if value:
                rows.append((label, str(value)))
    elif doc.child_id and doc.child:
        child = doc.child
        rows.append(('שם הלקוח', child.full_name or ''))
        family = getattr(child, 'family', None)
        if family is not None:
            for attr, label in (('phone', 'טלפון'), ('email', 'אימייל')):
                value = getattr(family, attr, '') or ''
                if value:
                    rows.append((label, str(value)))
    return rows


def _pairs_table(pairs: list[tuple[str, str]], styles: dict, width: float) -> Table:
    label_w = 3.2 * cm
    data = [[_p(value, styles['value']), _p(label, styles['label'])] for label, value in pairs]
    table = Table(data, colWidths=[width - label_w, label_w])
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2),
    ]))
    return table


def _header_card(doc: FormalDocument, styles: dict) -> Table:
    issued_at = doc.document_date.strftime('%d/%m/%Y')
    if doc.created_at:
        issued_at += ' ' + doc.created_at.strftime('%H:%M')
    doc_pairs = [('מספר מסמך', doc.document_number), ('תאריך ושעה', issued_at)]
    doc_pairs += _customer_details(doc)
    if doc.due_date:
        doc_pairs.append(('תאריך פירעון', doc.due_date.strftime('%d/%m/%Y')))
    if doc.description:
        doc_pairs.append(('פרטים', doc.description))
    if doc.document_type == 'credit_invoice':
        linked = doc.linked_document.document_number if doc.linked_document_id else doc.linked_document_number
        if linked:
            doc_pairs.append(('זיכוי עבור מסמך', linked))
    if doc.document_type == 'draft':
        doc_pairs.append(('יהפוך ל', TYPE_LABELS.get(doc.draft_target_type, doc.draft_target_type or '')))

    biz_pairs = [
        ('שם העסק', ISSUER_NAME),
        ('עוסק מורשה / ח.פ.', ISSUER_COMPANY_NUMBER),
        ('כתובת', ISSUER_ADDRESS),
        ('טלפון', ISSUER_PHONE),
    ]
    half = (CONTENT_WIDTH - 0.6 * cm) / 2
    left = [_p('פרטי העסק', styles['section']), Spacer(1, 0.15 * cm), _pairs_table(biz_pairs, styles, half - 0.4 * cm)]
    right = [_p('פרטי המסמך והלקוח', styles['section']), Spacer(1, 0.15 * cm), _pairs_table(doc_pairs, styles, half - 0.4 * cm)]
    card = Table([[left, right]], colWidths=[half, half])
    card.setStyle(_card([('BACKGROUND', (0, 0), (-1, -1), PANEL_BG)], padding=(9, 9, 10, 10)))
    return card


# --- what was sold ------------------------------------------------------------

def _items_table(doc: FormalDocument, styles: dict) -> Table:
    price_word = 'כולל מע"מ' if doc.prices_include_vat else 'לפני מע"מ'
    # Columns run left-to-right on the page, so the description sits at the
    # right edge by being the last column.
    widths = [3.0 * cm, 3.0 * cm, 1.8 * cm, CONTENT_WIDTH - 7.8 * cm]
    header = [
        _p('סה"כ שורה ' + price_word, styles['th']),
        _p('מחיר יחידה ' + price_word, styles['th']),
        _p('כמות', styles['th']),
        _p('תיאור פריט / שירות', styles['th']),
    ]
    data = [header]
    items = list(doc.line_items.all())
    for item in items:
        desc = item.description or item.sku or 'פריט'
        cell = [_p(desc, styles['td'])]
        if item.sku and item.description:
            cell.append(_p(f'מק"ט {item.sku}', styles['sub']))
        qty = item.quantity.normalize()
        qty_text = f'{qty:f}' if qty != qty.to_integral() else str(int(qty))
        data.append([
            _p(_money(item.line_total), styles['td_bold']),
            _p(_money(item.unit_price), styles['td_num']),
            _p(qty_text, styles['td_num']),
            cell,
        ])
    if not items:
        data.append([
            _p(_money(doc.subtotal), styles['td_bold']), _p(_money(doc.subtotal), styles['td_num']),
            _p('1', styles['td_num']), _p(doc.description or 'שירות', styles['td']),
        ])
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_NAVY),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
        ('LINEBELOW', (0, 1), (-1, -2), 0.35, BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ], padding=(6, 6, 6, 6)))
    return table


# --- money ----------------------------------------------------------------------

def _totals_card(doc: FormalDocument, styles: dict, width: float) -> Table:
    net = doc.subtotal - doc.discount_amount
    rows = []
    if doc.discount_amount:
        rows.append(('סכום לפני הנחה', _money(doc.subtotal)))
        rows.append(('הנחה', '-' + _money(doc.discount_amount)))
    rows.append(('סה"כ לפני מע"מ', _money(net)))
    if doc.vat_exempt:
        rows.append(('מע"מ', 'פטור / ללא מע"מ'))
    else:
        pct = doc.vat_percent.normalize()
        pct_text = f'{pct:f}' if pct != pct.to_integral() else str(int(pct))
        rows.append((f'מע"מ {pct_text}%', _money(doc.vat_amount)))
    data = [[_p(value, styles['value']), _p(label, styles['label'])] for label, value in rows]
    grand_word = 'סה"כ זיכוי' if doc.document_type == 'credit_invoice' else 'סה"כ לתשלום'
    data.append([_p(_money(doc.total_amount), styles['grand_value']), _p(grand_word, styles['grand_label'])])
    table = Table(data, colWidths=[width * 0.45, width * 0.55])
    table.setStyle(_card([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#ecebf7')),
        ('LINEABOVE', (0, -1), (-1, -1), 0.8, BRAND_PURPLE),
        ('TOPPADDING', (0, -1), (-1, -1), 8),
    ], padding=(4, 4, 10, 10)))
    return table


def _payment_card(doc: FormalDocument, styles: dict, width: float) -> Table:
    payments = list(doc.payments.all())
    rows: list[tuple[str, str]] = []
    if doc.document_type == 'draft':
        rows.append(('סטטוס', 'טיוטה — טרם הופק'))
    elif doc.document_type == 'credit_invoice':
        rows.append(('סטטוס', 'זיכוי'))
        if doc.credit_reason:
            rows.append(('סיבת הזיכוי', doc.credit_reason))
    elif payments:
        rows.append(('סטטוס', 'שולם'))
        for p in payments:
            label = PAYMENT_METHOD_LABELS.get(p.payment_method, p.get_payment_method_display() if hasattr(p, 'get_payment_method_display') else p.payment_method)
            detail = label
            if p.card_last_four:
                detail += f' ****{p.card_last_four}'
            if p.card_installments and p.card_installments > 1:
                detail += f' · {p.card_installments} תשלומים'
            if p.reference:
                detail += f' · {p.reference}'
            rows.append((_money(p.amount), detail))
        paid = sum((p.amount for p in payments), Decimal('0'))
        rows.append(('יתרה לתשלום', _money(max(doc.total_amount - paid, Decimal('0')))))
    else:
        rows.append(('סטטוס', 'ממתין לתשלום'))
        if doc.payment_terms:
            rows.append(('תנאי תשלום', doc.payment_terms))
        rows.append(('יתרה לתשלום', _money(doc.total_amount)))
    body = [_p('פרטי תשלום', styles['section']), Spacer(1, 0.1 * cm), _pairs_table(rows, styles, width - 0.6 * cm)]
    if doc.customer_notes:
        body += [Spacer(1, 0.1 * cm), _p(doc.customer_notes, styles['note'])]
    table = Table([[body]], colWidths=[width])
    table.setStyle(_card([('BACKGROUND', (0, 0), (-1, -1), PANEL_BG)], padding=(8, 8, 10, 10)))
    return table


# --- the small print -----------------------------------------------------------

def _notes(doc: FormalDocument, styles: dict) -> list:
    out = []
    if doc.document_type == 'draft':
        out.append(_p('טיוטה: מסמך זה אינו חשבונית ואינו מסמך מס. הוא יקבל מספר רק לאחר אישור.', styles['credit']))
    elif doc.document_type in TAX_DOCUMENT_TYPES:
        net = doc.subtotal - doc.discount_amount
        if net >= ALLOCATION_THRESHOLD:
            out.append(_p('מספר הקצאה: נדרש לעסקה זו (סכום לפני מע"מ מעל 5,000 ₪) — טרם הוזן.', styles['credit']))
        else:
            out.append(_p('מספר הקצאה: לא נדרש לעסקה זו — סכום העסקה לפני מע"מ נמוך מ-5,000 ₪.', styles['note']))
    elif doc.document_type == 'transaction_invoice':
        out.append(_p('חשבון עסקה אינו חשבונית מס. חשבונית מס תופק עם התשלום.', styles['note']))
    if doc.document_type != 'draft':
        out.append(_p('מסמך ממוחשב: מסמך זה הופק באופן דיגיטלי.', styles['note']))
    return out


def _draw_page(doc_obj: FormalDocument):
    def on_page(canvas, document):
        canvas.saveState()
        # Footer: the issuer's line, and the only e-mail that goes on an invoice.
        canvas.setStrokeColor(BRAND_PURPLE)
        canvas.setLineWidth(0.8)
        y = BOTTOM_MARGIN - 0.35 * cm
        canvas.line(SIDE_MARGIN, y, PAGE_WIDTH - SIDE_MARGIN, y)
        canvas.setFont('Heebo', 8.5)
        canvas.setFillColor(BRAND_NAVY)
        footer = f'{ISSUER_NAME} · {ISSUER_ADDRESS} · {ISSUER_PHONE} · {ISSUER_EMAIL}'
        canvas.drawCentredString(PAGE_WIDTH / 2, y - 0.45 * cm, _rtl(footer))
        canvas.setFont('Heebo', 7.5)
        canvas.setFillColor(colors.HexColor('#8a8da3'))
        canvas.drawRightString(PAGE_WIDTH - SIDE_MARGIN, y - 0.95 * cm, _rtl(f'עמוד {document.page}'))
        if doc_obj.document_type == 'draft':
            # A draft says so across the whole page, so a printout can never
            # pass for the real thing.
            canvas.setFont('Heebo-Bold', 92)
            canvas.setFillColor(colors.Color(0.55, 0.55, 0.65, alpha=0.13))
            canvas.translate(PAGE_WIDTH / 2, PAGE_HEIGHT / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, _rtl('טיוטה'))
        canvas.restoreState()
    return on_page


def generate_document_pdf(doc: FormalDocument) -> bytes:
    _ensure_fonts_registered()
    styles = _styles()
    label = TYPE_LABELS.get(doc.document_type, doc.document_type)

    story = []
    if os.path.isfile(_LOGO_IMAGE):
        logo_w = 3.4 * cm
        logo = Image(_LOGO_IMAGE, width=logo_w, height=logo_w * LOGO_RATIO)
        logo.hAlign = 'CENTER'
        story += [logo, Spacer(1, 0.4 * cm)]

    story.append(_p(f'{label} - {doc.document_number}', styles['title']))
    if doc.document_type == 'draft':
        story.append(_p('טיוטה — אינו מסמך מס', styles['origin']))
    else:
        story.append(_p('מקור', styles['origin']))
    story.append(Spacer(1, 0.5 * cm))

    story.append(_header_card(doc, styles))
    story.append(Spacer(1, 0.55 * cm))

    story.append(_p('פירוט העסקה', styles['section']))
    story.append(Spacer(1, 0.15 * cm))
    story.append(_items_table(doc, styles))
    story.append(Spacer(1, 0.5 * cm))

    half = (CONTENT_WIDTH - 0.6 * cm) / 2
    bottom = Table(
        [[_totals_card(doc, styles, half), _payment_card(doc, styles, half)]],
        colWidths=[half + 0.3 * cm, half + 0.3 * cm],
    )
    bottom.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(bottom)
    story.append(Spacer(1, 0.7 * cm))
    story.extend(_notes(doc, styles))

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=SIDE_MARGIN, rightMargin=SIDE_MARGIN,
        topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
        title=f'{label} {doc.document_number}', author=ISSUER_NAME,
    )
    on_page = _draw_page(doc)
    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buffer.getvalue()
