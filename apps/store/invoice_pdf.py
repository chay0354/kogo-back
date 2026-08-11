"""Generate a printable PDF receipt for a store invoice."""
from __future__ import annotations

import io
import os
from decimal import Decimal

from bidi.algorithm import get_display
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.store.models import StoreInvoice

PAGE_MARGIN = 1.8 * cm
_FONTS_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'scheduling', 'rental_agreement', 'fonts',
)
_FONTS_REGISTERED = False

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


def generate_store_invoice_pdf(invoice: StoreInvoice) -> bytes:
    _ensure_fonts_registered()
    invoice = (
        StoreInvoice.objects
        .select_related('child')
        .prefetch_related('line_items__product')
        .get(pk=invoice.pk)
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=PAGE_MARGIN,
        leftMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        title=invoice.invoice_number,
    )

    title_style = ParagraphStyle(
        'Title', fontName='Heebo-Bold', fontSize=18, alignment=TA_CENTER, leading=22,
    )
    label_style = ParagraphStyle(
        'Label', fontName='Heebo-Bold', fontSize=10, alignment=TA_RIGHT, leading=14,
    )
    value_style = ParagraphStyle(
        'Value', fontName='Heebo', fontSize=10, alignment=TA_RIGHT, leading=14,
    )

    customer = invoice.child.full_name if invoice.child_id else (invoice.customer_name or 'לקוח/ה')
    issue = timezone.localtime(invoice.issue_date).strftime('%d/%m/%Y %H:%M')
    paid = invoice.amount_paid if invoice.amount_paid else (
        invoice.total_amount if invoice.payment_status == 'completed' else Decimal('0.00')
    )
    open_balance = max(Decimal('0'), Decimal(str(invoice.total_amount)) - Decimal(str(paid)))

    story = [
        Paragraph(_rtl('קוגומלו — חשבונית חנות'), title_style),
        Spacer(1, 0.4 * cm),
        Paragraph(_rtl(f'מספר חשבונית: {invoice.invoice_number}'), value_style),
        Paragraph(_rtl(f'תאריך: {issue}'), value_style),
        Paragraph(_rtl(f'לקוח: {customer}'), value_style),
    ]
    if invoice.customer_phone:
        story.append(Paragraph(_rtl(f'טלפון: {invoice.customer_phone}'), value_style))
    if invoice.customer_email:
        story.append(Paragraph(_rtl(f'אימייל: {invoice.customer_email}'), value_style))
    if invoice.website_order_number:
        story.append(Paragraph(_rtl(f'הזמנת אתר: {invoice.website_order_number}'), value_style))

    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(_rtl('פריטים'), label_style))
    story.append(Spacer(1, 0.15 * cm))

    rows = [[
        Paragraph(_rtl('מוצר'), label_style),
        Paragraph(_rtl('כמות'), label_style),
        Paragraph(_rtl('מחיר'), label_style),
        Paragraph(_rtl('סה"כ'), label_style),
    ]]
    for sale in invoice.line_items.all():
        name = sale.product.name if sale.product_id else 'פריט'
        if sale.size:
            name = f'{name} ({sale.size})'
        rows.append([
            Paragraph(_rtl(name), value_style),
            Paragraph(_rtl(str(sale.quantity)), value_style),
            Paragraph(_rtl(_money(sale.unit_price)), value_style),
            Paragraph(_rtl(_money(sale.total_price)), value_style),
        ])
    if len(rows) == 1:
        rows.append([
            Paragraph(_rtl('(אין פירוט שורות)'), value_style),
            Paragraph('', value_style),
            Paragraph('', value_style),
            Paragraph('', value_style),
        ])

    table = Table(rows, colWidths=[8.5 * cm, 2 * cm, 2.5 * cm, 2.5 * cm], hAlign='RIGHT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f7f6fc')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dddddd')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.extend([table, Spacer(1, 0.5 * cm)])

    summary_rows = [
        [_rtl('סכום כולל'), _rtl(_money(invoice.total_amount))],
        [_rtl('שולם עד כה'), _rtl(_money(paid))],
        [_rtl('יתרה פתוחה'), _rtl(_money(open_balance))],
        [_rtl('אמצעי תשלום'), _rtl(PAYMENT_METHOD_LABELS.get(invoice.payment_method, invoice.payment_method))],
        [_rtl('סטטוס'), _rtl(PAYMENT_STATUS_LABELS.get(invoice.payment_status, invoice.payment_status))],
    ]
    txn = (invoice.tranzila_confirmation_code or invoice.tranzila_transaction_id or '').strip()
    if txn:
        summary_rows.append([_rtl('אישור תשלום'), _rtl(txn)])

    summary = Table(summary_rows, colWidths=[5 * cm, 10 * cm], hAlign='RIGHT')
    summary.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Heebo-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Heebo'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(summary)

    doc.build(story)
    return buffer.getvalue()
