"""Generate a printable PDF receipt for a store invoice on Cogomelo letterhead."""
from __future__ import annotations

import io
import os
from decimal import Decimal

from bidi.algorithm import get_display
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.store.models import StoreInvoice

PAGE_WIDTH, PAGE_HEIGHT = A4
CONTENT_LEFT = 2.0 * cm
CONTENT_RIGHT = PAGE_WIDTH - 2.0 * cm
CONTENT_TOP = PAGE_HEIGHT - 5.2 * cm
CONTENT_BOTTOM = 3.4 * cm

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), 'assets', 'letterhead')
_BG_IMAGE = os.path.join(_ASSETS_DIR, 'image3.jpg')
_LOGO_IMAGE = os.path.join(_ASSETS_DIR, 'image2.png')
_FOOTER_IMAGE = os.path.join(_ASSETS_DIR, 'image1.png')

_FONTS_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'scheduling', 'rental_agreement', 'fonts',
)
_FONTS_REGISTERED = False

BRAND_PURPLE = colors.HexColor('#303094')
BRAND_NAVY = colors.HexColor('#25326a')
BRAND_ORANGE = colors.HexColor('#f4825a')
PANEL_BG = colors.HexColor('#f7f6fc')
BORDER = colors.HexColor('#ddd6f3')

PAYMENT_METHOD_LABELS = {
    'credit_card': 'אשראי',
    'cash': 'מזומן',
    'monthly_billing': 'הוראת קבע',
}

PAYMENT_STATUS_LABELS = {
    'pending': 'ממתין',
    'completed': 'שולם',
    'partially_paid': 'שולם חלקית',
    'failed': 'נכשל',
    'refunded': 'זוכה',
    'refund_failed': 'זיכוי נכשל',
}


def _ensure_fonts_registered() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont('Heebo', os.path.join(_FONTS_DIR, 'Heebo-Regular.ttf')))
    pdfmetrics.registerFont(TTFont('Heebo-Bold', os.path.join(_FONTS_DIR, 'Heebo-Bold.ttf')))
    _FONTS_REGISTERED = True


def _rtl(text: str) -> str:
    return get_display(text or '')


def _money(amount: Decimal | float) -> str:
    return f'₪{Decimal(str(amount)):.2f}'


def _draw_letterhead(canvas, _doc) -> None:
    """Paint the Word letterhead background, logo, and footer strip."""
    canvas.saveState()

    if os.path.isfile(_BG_IMAGE):
        canvas.drawImage(
            _BG_IMAGE,
            0,
            0,
            width=PAGE_WIDTH,
            height=PAGE_HEIGHT,
            preserveAspectRatio=False,
            mask='auto',
        )

    if os.path.isfile(_LOGO_IMAGE):
        logo_w = 5.6 * cm
        logo_h = logo_w * (524 / 656)
        canvas.drawImage(
            _LOGO_IMAGE,
            (PAGE_WIDTH - logo_w) / 2,
            PAGE_HEIGHT - logo_h - 0.55 * cm,
            width=logo_w,
            height=logo_h,
            preserveAspectRatio=True,
            mask='auto',
        )

    if os.path.isfile(_FOOTER_IMAGE):
        footer_w = PAGE_WIDTH - 1.2 * cm
        footer_h = footer_w * (384 / 2040)
        canvas.drawImage(
            _FOOTER_IMAGE,
            0.6 * cm,
            0.45 * cm,
            width=footer_w,
            height=footer_h,
            preserveAspectRatio=True,
            mask='auto',
        )

    canvas.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    return {
        'title': ParagraphStyle(
            'InvoiceTitle',
            fontName='Heebo-Bold',
            fontSize=22,
            textColor=BRAND_PURPLE,
            alignment=TA_CENTER,
            leading=26,
        ),
        'subtitle': ParagraphStyle(
            'InvoiceSubtitle',
            fontName='Heebo',
            fontSize=11,
            textColor=BRAND_NAVY,
            alignment=TA_CENTER,
            leading=14,
        ),
        'label': ParagraphStyle(
            'InvoiceLabel',
            fontName='Heebo-Bold',
            fontSize=10,
            textColor=BRAND_NAVY,
            alignment=TA_RIGHT,
            leading=14,
        ),
        'value': ParagraphStyle(
            'InvoiceValue',
            fontName='Heebo',
            fontSize=10,
            textColor=BRAND_NAVY,
            alignment=TA_RIGHT,
            leading=14,
        ),
        'total': ParagraphStyle(
            'InvoiceTotal',
            fontName='Heebo-Bold',
            fontSize=14,
            textColor=BRAND_PURPLE,
            alignment=TA_LEFT,
            leading=18,
        ),
    }


def _meta_panel(invoice: StoreInvoice, styles: dict[str, ParagraphStyle]) -> Table:
    customer = invoice.child.full_name if invoice.child_id else (invoice.customer_name or 'לקוח/ה')
    issue = timezone.localtime(invoice.issue_date).strftime('%d/%m/%Y %H:%M')

    left_rows = [
        [Paragraph(_rtl('פרטי חשבונית'), styles['label'])],
        [Paragraph(_rtl(f'מספר: {invoice.invoice_number}'), styles['value'])],
        [Paragraph(_rtl(f'תאריך: {issue}'), styles['value'])],
        [Paragraph(
            _rtl(PAYMENT_STATUS_LABELS.get(invoice.payment_status, invoice.payment_status)),
            styles['value'],
        )],
    ]
    right_rows = [
        [Paragraph(_rtl('פרטי לקוח'), styles['label'])],
        [Paragraph(_rtl(customer), styles['value'])],
    ]
    if invoice.customer_phone:
        right_rows.append([Paragraph(_rtl(f'טלפון: {invoice.customer_phone}'), styles['value'])])
    if invoice.customer_email:
        right_rows.append([Paragraph(_rtl(f'אימייל: {invoice.customer_email}'), styles['value'])])
    if invoice.website_order_number:
        right_rows.append([Paragraph(_rtl(f'הזמנה: {invoice.website_order_number}'), styles['value'])])

    panel = Table(
        [[Table(left_rows, colWidths=[7.8 * cm]), Table(right_rows, colWidths=[7.8 * cm])]],
        colWidths=[8.3 * cm, 8.3 * cm],
        hAlign='RIGHT',
    )
    panel.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL_BG),
        ('BOX', (0, 0), (-1, -1), 0.75, BORDER),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('INNERGRID', (0, 0), (-1, -1), 0, colors.white),
    ]))
    return panel


def _items_table(invoice: StoreInvoice, styles: dict[str, ParagraphStyle]) -> Table:
    rows = [[
        Paragraph(_rtl('מוצר'), styles['label']),
        Paragraph(_rtl('כמות'), styles['label']),
        Paragraph(_rtl('מחיר'), styles['label']),
        Paragraph(_rtl('סה"כ'), styles['label']),
    ]]
    for sale in invoice.line_items.all():
        name = sale.product.name if sale.product_id else 'פריט'
        if sale.size:
            name = f'{name} ({sale.size})'
        rows.append([
            Paragraph(_rtl(name), styles['value']),
            Paragraph(_rtl(str(sale.quantity)), styles['value']),
            Paragraph(_rtl(_money(sale.unit_price)), styles['value']),
            Paragraph(_rtl(_money(sale.total_price)), styles['value']),
        ])
    if len(rows) == 1:
        rows.append([
            Paragraph(_rtl('(אין פירוט שורות)'), styles['value']),
            Paragraph('', styles['value']),
            Paragraph('', styles['value']),
            Paragraph('', styles['value']),
        ])

    table = Table(rows, colWidths=[8.4 * cm, 1.8 * cm, 2.4 * cm, 2.4 * cm], hAlign='RIGHT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Heebo-Bold'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    return table


def _summary_block(invoice: StoreInvoice, styles: dict[str, ParagraphStyle]) -> Table:
    paid = invoice.amount_paid if invoice.amount_paid else (
        invoice.total_amount if invoice.payment_status == 'completed' else Decimal('0.00')
    )
    open_balance = max(Decimal('0'), Decimal(str(invoice.total_amount)) - Decimal(str(paid)))

    summary_rows = [
        [_rtl('אמצעי תשלום'), _rtl(PAYMENT_METHOD_LABELS.get(invoice.payment_method, invoice.payment_method))],
        [_rtl('שולם'), _rtl(_money(paid))],
    ]
    if open_balance > 0:
        summary_rows.append([_rtl('יתרה'), _rtl(_money(open_balance))])
    txn = (invoice.tranzila_confirmation_code or invoice.tranzila_transaction_id or '').strip()
    if txn:
        summary_rows.append([_rtl('אישור תשלום'), _rtl(txn)])

    summary = Table(summary_rows, colWidths=[4.5 * cm, 5.5 * cm], hAlign='LEFT')
    summary.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Heebo-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Heebo'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (-1, -1), BRAND_NAVY),
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))

    total_box = Table(
        [[Paragraph(_rtl('סה"כ לתשלום'), styles['label'])],
         [Paragraph(_rtl(_money(invoice.total_amount)), styles['total'])]],
        colWidths=[5.5 * cm],
        hAlign='LEFT',
    )
    total_box.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL_BG),
        ('BOX', (0, 0), (-1, -1), 1, BRAND_PURPLE),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
    ]))

    row = Table([[summary, total_box]], colWidths=[6.5 * cm, 6.0 * cm], hAlign='RIGHT')
    row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return row


def generate_store_invoice_pdf(invoice: StoreInvoice) -> bytes:
    _ensure_fonts_registered()
    invoice = (
        StoreInvoice.objects
        .select_related('child')
        .prefetch_related('line_items__product')
        .get(pk=invoice.pk)
    )

    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=CONTENT_LEFT,
        leftMargin=PAGE_WIDTH - CONTENT_RIGHT,
        topMargin=PAGE_HEIGHT - CONTENT_TOP,
        bottomMargin=CONTENT_BOTTOM,
        title=invoice.invoice_number,
    )

    story = [
        Paragraph(_rtl('חשבונית'), styles['title']),
        Paragraph(_rtl('קוגומלו — חנות מוצרים'), styles['subtitle']),
        Spacer(1, 0.35 * cm),
        _meta_panel(invoice, styles),
        Spacer(1, 0.45 * cm),
        Paragraph(_rtl('פירוט פריטים'), styles['label']),
        Spacer(1, 0.12 * cm),
        _items_table(invoice, styles),
        Spacer(1, 0.35 * cm),
        _summary_block(invoice, styles),
    ]

    doc.build(story, onFirstPage=_draw_letterhead, onLaterPages=_draw_letterhead)
    return buffer.getvalue()
