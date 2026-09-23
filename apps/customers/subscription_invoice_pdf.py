"""The receipt a family gets for a lesson charge, drawn by the shared layout.

This is the most common document the business issues. What it says is decided
here — the lines come from the checkout log where there is one, and from the
invoice's children where there is not, exactly as before. How it looks is
decided once, in ``apps.documents.invoice_layout``.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.core.vat import (
    DOCUMENT_TITLE, VAT_PERCENT_DISPLAY, split_vat_inclusive, split_vat_inclusive_lines,
)
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.documents.invoice_document import (
    allocation_note, business_fields, computerized_note, date_stamp, footer_line, issue_stamp,
    late_note, signature_note,
)
from apps.documents.invoice_layout import (
    Field, InvoiceLayout, LineItem, money, render_invoice_pdf,
)
from apps.documents.issuer import COPY_MARK, ISSUER_NAME, ORIGINAL_MARK

STATUS_LABELS = {
    'paid': 'שולם',
    'pending': 'ממתין לתשלום',
    'failed': 'נכשל',
    'refunded': 'זוכה',
    'cancelled': 'בוטל',
}


def _charged_lines(invoice: Invoice) -> list[tuple[str, str, Decimal]]:
    """
    (description, child, amount) for each line, as the existing code works them out.

    The checkout log is the source when the invoice has one; otherwise the
    invoice's children are, with the registration fee split off the single-child
    case. Nothing here is new — only the shape it is handed on in.
    """
    checkout_log = invoice.activity_logs.filter(action='checkout_lines').order_by('-created_at').first()
    checkout_lines = []
    if checkout_log and isinstance(checkout_log.details, dict):
        checkout_lines = checkout_log.details.get('lines') or []

    rows: list[tuple[str, str, Decimal]] = []
    if checkout_lines:
        for line in checkout_lines:
            child_name = str(line.get('child_name') or '')
            desc = str(line.get('description') or 'מנוי חוג')
            amount = Decimal(str(line.get('amount') or '0'))
            fee = Decimal(str(line.get('registration_fee') or '0'))
            lesson_part = amount - fee
            if fee > 0 and lesson_part > 0:
                rows.append((f'מנוי חודשי — {desc}', child_name, lesson_part))
                rows.append(('דמי רישום (חד-פעמי)', child_name, fee))
            else:
                rows.append((desc if fee <= 0 else f'דמי רישום — {desc}', child_name, amount))
        return rows

    payment = invoice.payment
    registration_fee = Decimal('0')
    if payment and payment.registration_fee:
        registration_fee = payment.registration_fee

    child_count = invoice.children.count()
    for entry in invoice.children.all():
        child_name = entry.child.full_name if entry.child_id else ''
        if entry.lesson_id and entry.course_id:
            desc = f'{entry.course.name} — {entry.lesson.get_day_of_week_display()}'
        elif entry.course_id:
            desc = entry.course.name
        else:
            desc = 'מנוי חוג'
        if registration_fee > 0 and payment and child_count <= 1:
            rows.append((f'מנוי חודשי — {desc}', child_name, payment.final_amount - registration_fee))
            rows.append(('דמי רישום (חד-פעמי)', child_name, registration_fee))
        else:
            rows.append((desc, child_name, invoice.amount if child_count <= 1 else Decimal('0')))

    if not rows:
        rows.append(('מנוי חוג', '', invoice.amount))
    return rows


def _items(invoice: Invoice) -> list[LineItem]:
    rows = _charged_lines(invoice)
    splits = split_vat_inclusive_lines([amount for _d, _c, amount in rows])
    rate = f'{VAT_PERCENT_DISPLAY:g}%'
    items = []
    for (desc, child, amount), (net, _vat) in zip(rows, splits):
        items.append(LineItem(
            description=desc,
            sub=child,
            quantity='1',
            unit_price=money(net),
            line_net=money(net),
            vat_rate=rate,
            line_gross=money(amount),
        ))
    return items


def _late_dates(invoice: Invoice) -> tuple[str, str]:
    """(issued, money received) from the 'issued late' log, or ('', '')."""
    entry = next((log for log in invoice.activity_logs.all() if log.action == 'issued_late'), None)
    if entry is None:
        return '', ''
    details = entry.details or {}
    return (details.get('document_issued_at') or '')[:10], (details.get('money_received_at') or '')[:10]


def build_subscription_invoice_layout(invoice: Invoice, *, copy: bool = False, signed: bool = False) -> InvoiceLayout:
    """The design's data for one lesson receipt. Separated out so tests can read it."""
    before_vat, vat_amount, gross = split_vat_inclusive(invoice.amount)
    payer = (invoice.payer_name or invoice.family.name or '').strip()
    email = (invoice.payer_email or invoice.family.email or '').strip()
    phone = (invoice.payer_phone or invoice.family.phone or '').strip()
    paid = invoice.status == 'paid'

    # A family is not an עוסק מורשה: the allocation line says so above the threshold.
    notes = [allocation_note(before_vat, to_business=False), computerized_note()]
    issued_late, money_received = _late_dates(invoice)
    if issued_late or money_received:
        notes.insert(0, late_note(issued_late, money_received))
    if signed and not copy:
        notes.append(signature_note())

    return InvoiceLayout(
        # Whatever the record holds — INV-20260815-A1B2C3D4 as readily as
        # IR-2026-000123. The number is never reshaped for the page.
        title=f'{DOCUMENT_TITLE} - {invoice.invoice_number}',
        copy_mark=COPY_MARK if copy else ORIGINAL_MARK,
        document_fields=[
            Field('מספר מסמך', invoice.invoice_number),
            Field('תאריך ושעה', issue_stamp(invoice.invoice_date)),
            Field('שם הלקוח', payer or 'לקוח/ה'),
            Field('טלפון', phone),
            Field('אימייל', email),
            Field('סניף', invoice.branch.name if invoice.branch_id else ''),
            # Where the payment date differs from the issue date, both are rows
            # of their own — an accountant reads them, and so does a search.
            Field('תאריך הפקת המסמך', date_stamp(issued_late)),
            Field('תאריך קבלת התשלום', date_stamp(money_received)),
        ],
        business_fields=business_fields(),
        items=_items(invoice),
        payment_fields=[
            Field('סטטוס', STATUS_LABELS.get(invoice.status, invoice.status or '')),
            Field('אמצעי תשלום', invoice.get_payment_method_display() if invoice.payment_method else ''),
            Field('אישור תשלום', (invoice.tranzila_transaction_id or '').strip()),
            Field('יתרה לתשלום', money(Decimal('0') if paid else invoice.amount)),
        ],
        totals=[
            Field('סה"כ לפני מע"מ', money(before_vat)),
            Field(f'מע"מ {VAT_PERCENT_DISPLAY:g}%', money(vat_amount)),
        ],
        grand_label='סה"כ לתשלום',
        grand_value=money(gross),
        notes=notes,
        footer=footer_line(),
        pdf_title=invoice.invoice_number,
        pdf_author=ISSUER_NAME,
    )


def generate_subscription_invoice_pdf(invoice: Invoice, *, copy: bool = False, signed: bool = False) -> bytes:
    invoice = (
        Invoice.objects
        .select_related('family', 'parent', 'branch', 'payment')
        .prefetch_related('children__child', 'children__course', 'children__lesson', 'activity_logs')
        .get(pk=invoice.pk)
    )
    return render_invoice_pdf(build_subscription_invoice_layout(invoice, copy=copy, signed=signed))


# The receipt's "מקור" left the system once — by mail (email_sent_at) or, when
# there was no mail to send, as the office's first download. Logged here so
# every later print says "העתק" (נספח ה'(א)(4): "מקור" on one copy only).
ORIGINAL_PRODUCED = 'original_produced'


def original_downloaded(invoice: Invoice) -> bool:
    return InvoiceActivityLog.objects.filter(invoice_id=invoice.pk, action=ORIGINAL_PRODUCED).exists()


def reproduce_subscription_invoice_pdf(invoice: Invoice, *, user=None) -> bytes:
    """
    The PDF the office downloads: the original the first time the original has
    not yet left the system, a copy marked "העתק" every time after.

    Once originals are signed and stored at issue (apps/documents/signing), the
    original is that stored file, so every download is a copy.
    """
    from apps.documents.signing.service import office_copy

    if office_copy():
        return generate_subscription_invoice_pdf(invoice, copy=True)
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        copy = bool(locked.email_sent_at) or original_downloaded(locked)
        if not copy:
            InvoiceActivityLog.objects.create(
                invoice=locked, action=ORIGINAL_PRODUCED,
                details={'via': 'download', 'by': getattr(user, 'email', '') or str(getattr(user, 'pk', '') or '')},
            )
    return generate_subscription_invoice_pdf(invoice, copy=copy)
