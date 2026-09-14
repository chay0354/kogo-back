"""Documents issued by hand — חשבונית מס, חשבונית מס/קבלה, קבלה, חשבונית עסקה,
הודעת זיכוי and the draft that is none of them yet.

What the document says is decided here; how it looks is decided once, in
``apps.documents.invoice_layout``, so this and the two other customer-facing
generators cannot drift apart.
"""
from __future__ import annotations

from decimal import Decimal

from apps.documents.invoice_document import (
    ALLOCATION_THRESHOLD,
    allocation_note,
    business_fields,
    computerized_note,
    credit_reference_note,
    date_stamp,
    footer_line,
    issue_stamp,
)
from apps.documents.invoice_layout import (
    Field, InvoiceLayout, LineItem, Note, money, render_invoice_pdf,
)
from apps.documents.issuer import ISSUER_NAME, ORIGINAL_MARK
from apps.documents.models import DOCUMENT_TYPE_CHOICES, FormalDocument

TYPE_LABELS = dict(DOCUMENT_TYPE_CHOICES)
TAX_DOCUMENT_TYPES = ('tax_invoice', 'combined', 'credit_invoice')

PAYMENT_METHOD_LABELS = {
    'cash': 'מזומן', 'credit_card': 'כרטיס אשראי', 'card': 'כרטיס אשראי',
    'bank_transfer': 'העברה בנקאית', 'check': "צ'ק", 'bit': 'ביט', 'other': 'אחר',
}


def _quantity_text(quantity: Decimal) -> str:
    value = Decimal(str(quantity or 0)).normalize()
    return str(int(value)) if value == value.to_integral() else f'{value:f}'


def _vat_rate_text(doc: FormalDocument) -> str:
    if doc.vat_exempt:
        return 'פטור'
    percent = Decimal(str(doc.vat_percent or 0)).normalize()
    percent_text = str(int(percent)) if percent == percent.to_integral() else f'{percent:f}'
    return f'{percent_text}%'


def _net_and_gross(amount: Decimal, doc: FormalDocument) -> tuple[Decimal, Decimal]:
    """
    (before VAT, with VAT) for a stored line amount.

    Whether the stored number already includes VAT is the document's own
    `prices_include_vat`; nothing here changes that, it only shows both sides.
    """
    value = Decimal(str(amount or 0))
    if doc.vat_exempt:
        return value, value
    rate = Decimal(str(doc.vat_percent or 0)) / Decimal('100')
    if doc.prices_include_vat:
        net = (value / (Decimal('1') + rate)).quantize(Decimal('0.01')) if rate else value
        return net, value
    return value, (value * (Decimal('1') + rate)).quantize(Decimal('0.01'))


def _customer_fields(doc: FormalDocument) -> list[Field]:
    """
    The customer, from whichever record the document points at.

    A document may name a business customer, a child, or only a typed name — and
    an old one may carry no company number, phone or email at all. Empty values
    are dropped by the layout rather than printed as a bare label.
    """
    if doc.business_customer_id and doc.business_customer:
        customer = doc.business_customer
        return [
            Field('שם הלקוח', customer.full_name or ''),
            Field('ח.פ. / ע.מ.', str(getattr(customer, 'company_number', '') or '')),
            Field('ת.ז.', str(getattr(customer, 'id_number', '') or '')),
            Field('טלפון', str(getattr(customer, 'phone', '') or '')),
            Field('אימייל', str(getattr(customer, 'email', '') or '')),
            Field('כתובת', str(getattr(customer, 'address', '') or '')),
        ]
    if doc.child_id and doc.child:
        family = getattr(doc.child, 'family', None)
        return [
            Field('שם הלקוח', doc.child.full_name or ''),
            Field('טלפון', str(getattr(family, 'phone', '') or '')),
            Field('אימייל', str(getattr(family, 'email', '') or '')),
        ]
    return [Field('שם הלקוח', doc.customer_name or '')]


def _document_fields(doc: FormalDocument) -> list[Field]:
    fields = [
        # Printed exactly as issued — TI-…, CR-…, or an older shape.
        Field('מספר מסמך', doc.document_number),
        Field('תאריך המסמך', date_stamp(doc.document_date)),
        Field('תאריך ושעה', issue_stamp(doc.created_at)),
        *_customer_fields(doc),
        Field('תאריך פירעון', date_stamp(doc.due_date)),
        Field('פרטים', doc.description or ''),
    ]
    if doc.document_type == 'credit_invoice':
        linked = doc.linked_document.document_number if doc.linked_document_id else doc.linked_document_number
        linked_date = doc.linked_document_date or (
            doc.linked_document.document_date if doc.linked_document_id else None
        )
        fields += [
            Field('זיכוי עבור מסמך', linked or ''),
            Field('תאריך המסמך המקורי', date_stamp(linked_date)),
            Field('סיבת הזיכוי', doc.credit_reason or ''),
        ]
    if doc.document_type == 'draft':
        fields.append(Field('יהפוך ל', TYPE_LABELS.get(doc.draft_target_type, doc.draft_target_type or '')))
    return fields


def _items(doc: FormalDocument) -> list[LineItem]:
    rate = _vat_rate_text(doc)
    items: list[LineItem] = []
    for item in doc.line_items.all():
        unit_net, _unit_gross = _net_and_gross(item.unit_price, doc)
        line_net, line_gross = _net_and_gross(item.line_total, doc)
        items.append(LineItem(
            description=item.description or item.sku or 'פריט',
            sub=f'מק"ט {item.sku}' if (item.sku and item.description) else '',
            quantity=_quantity_text(item.quantity),
            unit_price=money(unit_net),
            line_net=money(line_net),
            vat_rate=rate,
            line_gross=money(line_gross),
        ))
    if not items:
        line_net, line_gross = _net_and_gross(doc.subtotal, doc)
        items.append(LineItem(
            description=doc.description or 'שירות',
            quantity='1',
            unit_price=money(line_net),
            line_net=money(line_net),
            vat_rate=rate,
            line_gross=money(line_gross),
        ))
    return items


def _totals(doc: FormalDocument) -> list[Field]:
    net = doc.subtotal - doc.discount_amount
    rows = []
    if doc.discount_amount:
        rows.append(Field('סכום לפני הנחה', money(doc.subtotal)))
        rows.append(Field('הנחה', '-' + money(doc.discount_amount)))
    rows.append(Field('סה"כ לפני מע"מ', money(net)))
    if doc.vat_exempt:
        rows.append(Field('מע"מ', 'פטור / ללא מע"מ'))
    else:
        rows.append(Field(f'מע"מ {_vat_rate_text(doc)}', money(doc.vat_amount)))
    return rows


def _payment_fields(doc: FormalDocument) -> list[Field]:
    payments = list(doc.payments.all())
    if doc.document_type == 'draft':
        return [Field('סטטוס', 'טיוטה — טרם הופק')]
    if doc.document_type == 'credit_invoice':
        return [
            Field('סטטוס', 'זיכוי'),
            Field('סיבת הזיכוי', doc.credit_reason or ''),
            Field('סה"כ זיכוי', money(doc.total_amount)),
        ]
    if not payments:
        return [
            Field('סטטוס', 'ממתין לתשלום'),
            Field('תנאי תשלום', doc.payment_terms or ''),
            Field('יתרה לתשלום', money(doc.total_amount)),
        ]

    # One row per fact rather than one sentence per payment: the card's last
    # four and the confirmation number have to be findable, and a Hebrew phrase
    # with digits buried in it is neither easy to read nor easy to search.
    fields = [Field('סטטוס', 'שולם')]
    for index, payment in enumerate(payments, start=1):
        suffix = f' ({index})' if len(payments) > 1 else ''
        label = PAYMENT_METHOD_LABELS.get(
            payment.payment_method,
            payment.get_payment_method_display()
            if hasattr(payment, 'get_payment_method_display') else payment.payment_method,
        )
        installments = payment.card_installments or 0
        fields += [
            Field(f'אמצעי תשלום{suffix}', label or ''),
            Field(f'4 ספרות אחרונות{suffix}', payment.card_last_four or ''),
            Field(f'מספר תשלומים{suffix}', str(installments) if installments > 1 else ''),
            Field(f'אסמכתא / אישור{suffix}', payment.reference or ''),
            Field(f'סכום ששולם{suffix}', money(payment.amount)),
        ]
    paid = sum((p.amount for p in payments), Decimal('0'))
    fields.append(Field('יתרה לתשלום', money(max(doc.total_amount - paid, Decimal('0')))))
    return fields


def _notes(doc: FormalDocument) -> list[Note]:
    notes: list[Note] = []
    if doc.document_type == 'credit_invoice':
        linked = doc.linked_document.document_number if doc.linked_document_id else doc.linked_document_number
        linked_date = doc.linked_document_date or (
            doc.linked_document.document_date if doc.linked_document_id else None
        )
        reference = credit_reference_note(linked or '', linked_date, doc.credit_reason or '')
        if reference is not None:
            notes.append(reference)
    if doc.document_type == 'draft':
        notes.append(Note('טיוטה:', 'מסמך זה אינו חשבונית ואינו מסמך מס. הוא יקבל מספר רק לאחר אישור.'))
    elif doc.document_type in TAX_DOCUMENT_TYPES:
        notes.append(allocation_note(doc.subtotal - doc.discount_amount))
    elif doc.document_type == 'transaction_invoice':
        notes.append(Note('חשבון עסקה:', 'אינו חשבונית מס. חשבונית מס תופק עם התשלום.'))
    if doc.document_type != 'draft':
        notes.append(computerized_note())
    return notes


def build_document_layout(doc: FormalDocument) -> InvoiceLayout:
    """The design's data for one hand-issued document. Separated out so tests can read it."""
    label = TYPE_LABELS.get(doc.document_type, doc.document_type)
    is_draft = doc.document_type == 'draft'
    is_credit = doc.document_type == 'credit_invoice'
    price_word = 'כולל מע"מ' if doc.prices_include_vat else 'לפני מע"מ'
    return InvoiceLayout(
        title=f'{label} - {doc.document_number}',
        copy_mark='טיוטה — אינו מסמך מס' if is_draft else ORIGINAL_MARK,
        document_fields=_document_fields(doc),
        business_fields=business_fields(),
        items=_items(doc),
        items_heading=f'פירוט העסקה (מחירים {price_word})',
        payment_fields=_payment_fields(doc),
        payment_note=doc.customer_notes or '',
        totals=_totals(doc),
        grand_label='סה"כ זיכוי' if is_credit else 'סה"כ לתשלום',
        grand_value=money(doc.total_amount),
        notes=_notes(doc),
        footer=footer_line(),
        watermark='טיוטה' if is_draft else '',
        pdf_title=f'{label} {doc.document_number}',
        pdf_author=ISSUER_NAME,
    )


def generate_document_pdf(doc: FormalDocument) -> bytes:
    return render_invoice_pdf(build_document_layout(doc))


__all__ = [
    'ALLOCATION_THRESHOLD',
    'PAYMENT_METHOD_LABELS',
    'TAX_DOCUMENT_TYPES',
    'TYPE_LABELS',
    'build_document_layout',
    'generate_document_pdf',
]
