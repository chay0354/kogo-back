"""The PDF a store customer gets, drawn by the shared invoice layout.

What the document says is decided here; how it looks is decided once, in
``apps.documents.invoice_layout``, so this and the two other customer-facing
generators cannot drift apart.

The module also still holds the letterhead palette and the small Hebrew helpers
that ``apps.documents.period_report_pdf`` and ``apps.signatures.pdf`` import
from it. Those two documents are not part of this design, so their colours and
helpers are left exactly as they were.
"""
from __future__ import annotations

import os
from decimal import Decimal

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from apps.core.vat import (
    DOCUMENT_TITLE, VAT_PERCENT_DISPLAY, split_vat_inclusive, split_vat_inclusive_lines,
)
from apps.documents.invoice_document import (
    allocation_note, business_fields, computerized_note, footer_line, issue_stamp,
)
from apps.documents.invoice_layout import (
    Field, InvoiceLayout, LineItem, money as _money_shared, render_invoice_pdf,
)
from apps.documents.issuer import COPY_MARK, ISSUER_NAME, ORIGINAL_MARK
from apps.store.models import StoreInvoice

_FONTS_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'scheduling', 'rental_agreement', 'fonts',
)
_FONTS_REGISTERED = False

# The letterhead palette of the reports that are not part of this design.
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


def _customer_fields(invoice: StoreInvoice) -> list[Field]:
    """
    Who the sale was to, from whichever of the fields the record happens to hold.

    A walk-in from 2024 has a name and nothing else; a website order has an
    address and an order number. Empty values are dropped by the layout, so an
    old row never prints a label with nothing after it.
    """
    customer = invoice.child.full_name if invoice.child_id else (invoice.customer_name or '')
    return [
        Field('שם הלקוח', customer or 'לקוח/ה'),
        Field('טלפון', invoice.customer_phone or ''),
        Field('אימייל', invoice.customer_email or ''),
        Field('כתובת', invoice.shipping_address or ''),
        Field('מספר הזמנה', invoice.website_order_number or ''),
        Field('הערות הלקוח', invoice.customer_notes or ''),
    ]


def _items(invoice: StoreInvoice) -> list[LineItem]:
    """The sold lines. Store prices are VAT-inclusive (see apps.core.vat)."""
    sales = list(invoice.line_items.all())
    grosses = [Decimal(str(sale.total_price or 0)) for sale in sales]
    if not sales:
        # An old sale whose line items were never written still has a total.
        grosses = [Decimal(str(invoice.total_amount or 0))]
    splits = split_vat_inclusive_lines(grosses)
    rate = f'{VAT_PERCENT_DISPLAY:g}%'

    if not sales:
        net, _vat = splits[0]
        return [LineItem(
            description='פריטי החנות בהזמנה זו',
            sub='פירוט השורות לא נשמר במסמך המקורי',
            quantity='1',
            unit_price=_money_shared(net),
            line_net=_money_shared(net),
            vat_rate=rate,
            line_gross=_money_shared(grosses[0]),
        )]

    items: list[LineItem] = []
    for sale, (net, _vat) in zip(sales, splits):
        name = sale.product.name if sale.product_id else 'פריט'
        if sale.size:
            name = f'{name} ({sale.size})'
        quantity = sale.quantity or 1
        unit_net = (net / quantity) if quantity else net
        items.append(LineItem(
            description=name,
            quantity=str(quantity),
            unit_price=_money_shared(unit_net.quantize(Decimal('0.01'))),
            line_net=_money_shared(net),
            vat_rate=rate,
            line_gross=_money_shared(sale.total_price),
        ))
    return items


# A sale that was paid when its document was issued. A refund afterwards does
# not change that document — it is a credit note of its own (הוראה 23(ב)), and a
# reprint is "הזהה במהותו למקור" (הוראה 18(ב)(2)). So a refunded sale prints as
# it was issued: paid, nothing credited on its face.
ISSUED_PAID = ('completed', 'refunded', 'refund_failed')


def _payment_fields(invoice: StoreInvoice) -> list[Field]:
    if invoice.payment_method != 'monthly_billing' and invoice.payment_status in ISSUED_PAID:
        status = 'completed'
        paid = Decimal(str(invoice.total_amount))
    else:
        status = invoice.payment_status
        paid = invoice.amount_paid if invoice.amount_paid else (
            invoice.total_amount if invoice.payment_status == 'completed' else Decimal('0.00')
        )
    open_balance = max(Decimal('0'), Decimal(str(invoice.total_amount)) - Decimal(str(paid)))
    method = PAYMENT_METHOD_LABELS.get(invoice.payment_method, invoice.payment_method or '')
    confirmation = (invoice.tranzila_confirmation_code or invoice.tranzila_transaction_id or '').strip()
    return [
        Field('סטטוס', PAYMENT_STATUS_LABELS.get(status, status or '')),
        Field('אמצעי תשלום', method),
        Field('אישור תשלום', confirmation),
        Field('שולם', _money_shared(paid)),
        Field('יתרה לתשלום', _money_shared(open_balance)),
    ]


def build_store_invoice_layout(invoice: StoreInvoice, *, copy: bool = False) -> InvoiceLayout:
    """The design's data for one store sale. Separated out so tests can read it."""
    before_vat, vat_amount, gross = split_vat_inclusive(invoice.total_amount)
    # A sale billed to the monthly standing order is not yet a receipt.
    title_word = 'חשבונית עסקה' if invoice.payment_method == 'monthly_billing' else DOCUMENT_TITLE
    return InvoiceLayout(
        # The number prints exactly as it was issued, whatever its shape.
        title=f'{title_word} - {invoice.invoice_number}',
        copy_mark=COPY_MARK if copy else ORIGINAL_MARK,
        document_fields=[
            Field('מספר מסמך', invoice.invoice_number),
            Field('תאריך ושעה', issue_stamp(invoice.issue_date)),
            *_customer_fields(invoice),
        ],
        business_fields=business_fields(),
        items=_items(invoice),
        payment_fields=_payment_fields(invoice),
        totals=[
            Field('סה"כ לפני מע"מ', _money_shared(before_vat)),
            Field(f'מע"מ {VAT_PERCENT_DISPLAY:g}%', _money_shared(vat_amount)),
        ],
        grand_label='סה"כ לתשלום',
        grand_value=_money_shared(gross),
        # A sale on monthly billing is a חשבונית עסקה, not a tax invoice: no
        # allocation line. A store buyer is a private customer.
        notes=[
            note for note in (
                None if invoice.payment_method == 'monthly_billing'
                else allocation_note(before_vat, to_business=False),
                computerized_note(),
            ) if note is not None
        ],
        footer=footer_line(),
        pdf_title=invoice.invoice_number,
        pdf_author=ISSUER_NAME,
    )


def generate_store_invoice_pdf(invoice: StoreInvoice, *, copy: bool = False) -> bytes:
    invoice = (
        StoreInvoice.objects
        .select_related('child')
        .prefetch_related('line_items__product')
        .get(pk=invoice.pk)
    )
    return render_invoice_pdf(build_store_invoice_layout(invoice, copy=copy))
