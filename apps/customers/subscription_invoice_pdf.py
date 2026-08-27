"""Generate branded letterhead PDF for CRM subscription invoices (financial Invoice)."""
from __future__ import annotations

import io
from decimal import Decimal

from bidi.algorithm import get_display
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.customers.financial_models import Invoice
from apps.core.vat import DOCUMENT_TITLE, split_vat_inclusive
from apps.store.invoice_pdf import (
    BORDER,
    BRAND_NAVY,
    BRAND_PURPLE,
    CONTENT_BOTTOM,
    CONTENT_LEFT,
    CONTENT_RIGHT,
    CONTENT_TOP,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    PANEL_BG,
    _draw_letterhead,
    _ensure_fonts_registered,
    _money,
    _rtl,
)


def generate_subscription_invoice_pdf(invoice: Invoice) -> bytes:
    invoice = (
        Invoice.objects
        .select_related('family', 'parent', 'branch', 'payment')
        .prefetch_related('children__child', 'children__course', 'children__lesson', 'activity_logs')
        .get(pk=invoice.pk)
    )

    _ensure_fonts_registered()
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

    title_style = ParagraphStyle(
        'SubInvTitle', fontName='Heebo-Bold', fontSize=22,
        textColor=BRAND_PURPLE, alignment=TA_CENTER, leading=26,
    )
    label_style = ParagraphStyle(
        'SubInvLabel', fontName='Heebo-Bold', fontSize=10,
        textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=14,
    )
    value_style = ParagraphStyle(
        'SubInvValue', fontName='Heebo', fontSize=10,
        textColor=BRAND_NAVY, alignment=TA_RIGHT, leading=14,
    )

    issue = timezone.localtime(invoice.invoice_date).strftime('%d/%m/%Y %H:%M')
    payer = (invoice.payer_name or invoice.family.name or 'לקוח/ה').strip()
    email = (invoice.payer_email or invoice.family.email or '').strip()
    phone = (invoice.payer_phone or invoice.family.phone or '').strip()

    story = [
        Paragraph(_rtl(DOCUMENT_TITLE), title_style),
        Paragraph(_rtl('קוגומלו — מנוי לחוג'), ParagraphStyle(
            'Sub', fontName='Heebo', fontSize=11, textColor=BRAND_NAVY, alignment=TA_CENTER,
        )),
        Paragraph(_rtl('המחירים כוללים מע"מ'), ParagraphStyle(
            'SubVat', fontName='Heebo', fontSize=10, textColor=BRAND_NAVY, alignment=TA_CENTER,
        )),
        Spacer(1, 0.35 * cm),
    ]

    meta_rows = [
        [Paragraph(_rtl('פרטי חשבונית'), label_style)],
        [Paragraph(_rtl(f'מספר: {invoice.invoice_number}'), value_style)],
        [Paragraph(_rtl(f'תאריך: {issue}'), value_style)],
        [Paragraph(_rtl('שולם'), value_style)],
    ]
    payer_rows = [
        [Paragraph(_rtl('פרטי משלם'), label_style)],
        [Paragraph(_rtl(payer), value_style)],
    ]
    if phone:
        payer_rows.append([Paragraph(_rtl(f'טלפון: {phone}'), value_style)])
    if email:
        payer_rows.append([Paragraph(_rtl(f'אימייל: {email}'), value_style)])

    meta = Table(
        [[Table(meta_rows, colWidths=[7.8 * cm]), Table(payer_rows, colWidths=[7.8 * cm])]],
        colWidths=[8.3 * cm, 8.3 * cm],
        hAlign='RIGHT',
    )
    meta.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL_BG),
        ('BOX', (0, 0), (-1, -1), 0.75, BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.extend([meta, Spacer(1, 0.45 * cm)])

    rows = [[
        Paragraph(_rtl('פריט'), label_style),
        Paragraph(_rtl('ילד'), label_style),
        Paragraph(_rtl('סה"כ'), label_style),
    ]]
    checkout_log = invoice.activity_logs.filter(action='checkout_lines').order_by('-created_at').first()
    checkout_lines = []
    if checkout_log and isinstance(checkout_log.details, dict):
        checkout_lines = checkout_log.details.get('lines') or []

    if checkout_lines:
        for line in checkout_lines:
            child_name = str(line.get('child_name') or '—')
            desc = str(line.get('description') or 'מנוי חוג')
            amount = Decimal(str(line.get('amount') or '0'))
            fee = Decimal(str(line.get('registration_fee') or '0'))
            lesson_part = amount - fee
            if fee > 0 and lesson_part > 0:
                rows.append([
                    Paragraph(_rtl(f'מנוי חודשי — {desc}'), value_style),
                    Paragraph(_rtl(child_name), value_style),
                    Paragraph(_rtl(_money(lesson_part)), value_style),
                ])
                rows.append([
                    Paragraph(_rtl('דמי רישום (חד-פעמי)'), value_style),
                    Paragraph(_rtl(child_name), value_style),
                    Paragraph(_rtl(_money(fee)), value_style),
                ])
            else:
                rows.append([
                    Paragraph(_rtl(desc if fee <= 0 else f'דמי רישום — {desc}'), value_style),
                    Paragraph(_rtl(child_name), value_style),
                    Paragraph(_rtl(_money(amount)), value_style),
                ])
    else:
        payment = invoice.payment
        registration_fee = Decimal('0')
        if payment and payment.registration_fee:
            registration_fee = payment.registration_fee

        for entry in invoice.children.all():
            child_name = entry.child.full_name if entry.child_id else '—'
            if entry.lesson_id and entry.course_id:
                desc = f'{entry.course.name} — {entry.lesson.get_day_of_week_display()}'
            elif entry.course_id:
                desc = entry.course.name
            else:
                desc = 'מנוי חוג'
            line_amount = invoice.amount
            if registration_fee > 0 and payment and invoice.children.count() <= 1:
                lesson_part = payment.final_amount - registration_fee
                rows.append([
                    Paragraph(_rtl(f'מנוי חודשי — {desc}'), value_style),
                    Paragraph(_rtl(child_name), value_style),
                    Paragraph(_rtl(_money(lesson_part)), value_style),
                ])
                rows.append([
                    Paragraph(_rtl('דמי רישום (חד-פעמי)'), value_style),
                    Paragraph(_rtl(child_name), value_style),
                    Paragraph(_rtl(_money(registration_fee)), value_style),
                ])
            else:
                rows.append([
                    Paragraph(_rtl(desc), value_style),
                    Paragraph(_rtl(child_name), value_style),
                    Paragraph(_rtl(_money(line_amount if invoice.children.count() <= 1 else Decimal('0'))), value_style),
                ])

    if len(rows) == 1:
        rows.append([
            Paragraph(_rtl('מנוי חוג'), value_style),
            Paragraph('', value_style),
            Paragraph(_rtl(_money(invoice.amount)), value_style),
        ])

    table = Table(rows, colWidths=[9.5 * cm, 3.5 * cm, 2.5 * cm], hAlign='RIGHT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND_PURPLE),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.extend([Paragraph(_rtl('פירוט'), label_style), Spacer(1, 0.12 * cm), table, Spacer(1, 0.35 * cm)])

    before_vat, vat_amount, gross = split_vat_inclusive(invoice.amount)
    total_box = Table(
        [
            [Paragraph(_rtl('סה"כ לפני מע"מ'), label_style),
             Paragraph(_rtl(_money(before_vat)), value_style)],
            [Paragraph(_rtl('מע"מ 18%'), label_style),
             Paragraph(_rtl(_money(vat_amount)), value_style)],
            [Paragraph(_rtl('סה"כ כולל מע"מ'), label_style),
             Paragraph(_rtl(_money(gross)), ParagraphStyle(
                 'Tot', fontName='Heebo-Bold', fontSize=14, textColor=BRAND_PURPLE, alignment=TA_RIGHT,
             ))],
        ],
        colWidths=[4.2 * cm, 2.8 * cm],
        hAlign='RIGHT',
    )
    total_box.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL_BG),
        ('BOX', (0, 0), (-1, -1), 1, BRAND_PURPLE),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(total_box)

    txn = (invoice.tranzila_transaction_id or '').strip()
    if txn:
        story.extend([
            Spacer(1, 0.25 * cm),
            Paragraph(_rtl(f'אישור תשלום: {txn}'), value_style),
        ])

    doc.build(story, onFirstPage=_draw_letterhead, onLaterPages=_draw_letterhead)
    return buffer.getvalue()
