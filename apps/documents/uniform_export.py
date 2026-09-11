"""The documents register in the Tax Authority's uniform structure (מבנה אחיד).

uniform_format.py writes the files from plain values and knows nothing about
Kogo. This reads a period's documents off the register the period report prints
(register.py) — every channel, the numbers that never became a document left
out — and hands the formatter what each record asks for: the header's amounts,
the lines, and on a receipt how it was paid.

One tax year at a time: the INI declares a single period, and a range across
years would put two years of the same runs in one file.

Read-only, like the report. Amounts are the register's, which are the stored
columns; the only thing worked out here is a line's price before VAT where the
document stored it with VAT in, because a D110 line is "before VAT" (1265).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.core.vat import VAT_PERCENT_DISPLAY
from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_NAME
from apps.documents.period_report import ReportInputError, build_report
from apps.documents.register import (
    CHANNEL_LESSONS,
    CHANNEL_MANUAL,
    CHANNEL_STORE,
    register_rows,
    series_of,
)
from apps.documents.uniform_format import (
    CARD_INSTALLMENTS,
    CARD_REGULAR,
    DOCUMENT_TYPE_CODES,
    PAYMENT_DOCUMENT_TYPES,
    UniformAddress,
    UniformBusiness,
    UniformDocument,
    UniformLine,
    UniformPayment,
    build_uniform_files,
    uniform_zip,
)

SOFTWARE_VERSION = '2026.09'
CENT = Decimal('0.01')
ZERO = Decimal('0.00')
RECEIPT_CODE = DOCUMENT_TYPE_CODES['receipt']
# 1257 holds up to twenty characters; a longer number is left off, never cut.
LINK_WIDTH = 20


def _digits(value) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


# The issuer printed on every document (issuer.py), with its address split the
# way A000 asks for it: 'רפאל איתן 5, קניון ספיר, קומה 1, פתח תקווה'.
BUSINESS = UniformBusiness(
    vat_number=_digits(ISSUER_COMPANY_NUMBER),
    name=ISSUER_NAME,
    software_version=SOFTWARE_VERSION,
    company_number=_digits(ISSUER_COMPANY_NUMBER),
    address=UniformAddress(street='רפאל איתן', house_number='5', city='פתח תקווה'),
)

# The type a number's run is, for the document a credit note names.
_RUN_TYPES = {
    'IR': DOCUMENT_TYPE_CODES['combined'],
    'ST': DOCUMENT_TYPE_CODES['combined'],
    'IRM': DOCUMENT_TYPE_CODES['combined'],
    'SD': DOCUMENT_TYPE_CODES['transaction_invoice'],
    'TX': DOCUMENT_TYPE_CODES['transaction_invoice'],
    'TI': DOCUMENT_TYPE_CODES['tax_invoice'],
    'RC': DOCUMENT_TYPE_CODES['receipt'],
    'CR': DOCUMENT_TYPE_CODES['credit_invoice'],
}

_METHODS = {
    'credit_card': 'credit_card',
    'cash': 'cash',
    'check': 'check',
    'bank_transfer': 'bank_transfer',
}


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def _paid(method: str, amount: Decimal, on, *, installments: int = 1, **check) -> UniformPayment:
    """One D120 row. A card's date is the day it was charged; a check's is its own."""
    code = _METHODS.get(method, 'other')
    card = code == 'credit_card'
    return UniformPayment(
        method=code,
        amount=amount,
        due_date=check.pop('due_date', None) if code == 'check' else (on if card else None),
        card_transaction_type=(CARD_INSTALLMENTS if installments > 1 else CARD_REGULAR) if card else None,
        **(check if code == 'check' else {}),
    )


def _document(row, type_code: int, lines, payments, *, customer_vat: str = '',
              linked: tuple = (None, '')) -> UniformDocument:
    if type_code == RECEIPT_CODE:
        # הבהרה 4: a receipt's amount received goes in 1219, 1221 and 1223.
        before = after = row.total_amount
        discount = vat = ZERO
    else:
        before, discount, after, vat = row.subtotal, row.discount_amount, row.net_amount, row.vat_amount
    linked_type, linked_number = linked
    return UniformDocument(
        type_code=type_code,
        number=row.document_number,
        issue_date=row.document_date,
        customer_name=row.customer,
        customer_vat_number=customer_vat,
        amount_before_discount=before,
        discount=discount,
        amount_after_discount=after,
        vat_amount=vat,
        total_amount=row.total_amount,
        linked_document_type=linked_type,
        linked_document_number=linked_number,
        lines=tuple(lines),
        payments=tuple(payments),
    )


def _one_line(row, vat_rate: Decimal, description: str) -> UniformLine:
    """A document stored without lines is written as one line of its whole amount."""
    return UniformLine(
        description=description or row.document_type_label,
        quantity=Decimal('1'),
        unit_price=row.subtotal,
        line_total=row.subtotal,
        vat_rate=vat_rate,
    )


def _linked(number: str, formal_types: dict) -> tuple:
    """(type, number) of the document a credit note names — or nothing, rather than a guess."""
    number = (number or '').strip()
    if not number or len(number) > LINK_WIDTH:
        return None, ''
    code = _RUN_TYPES.get(series_of(number))
    if code is None and number in formal_types:
        # A document numbered in the closed shared run: its own record says what it is.
        code = DOCUMENT_TYPE_CODES.get(formal_types[number])
    return (code, number) if code is not None else (None, '')


def _manual(row, doc, formal_types: dict) -> UniformDocument:
    type_code = DOCUMENT_TYPE_CODES[row.document_type]
    vat_rate = ZERO if doc.vat_exempt else Decimal(doc.vat_percent)
    lines = []
    for item in doc.line_items.all():
        quantity = Decimal(item.quantity)
        unit = Decimal(item.unit_price)
        if doc.prices_include_vat and vat_rate:
            # Typed with VAT in; a D110 line is before VAT (1265).
            unit = _cents(unit / (1 + vat_rate / 100))
        lines.append(UniformLine(
            description=item.description or item.sku or row.document_type_label,
            quantity=quantity,
            unit_price=unit,
            line_total=_cents(quantity * unit),
            vat_rate=vat_rate,
            catalog_number=item.sku or '',
        ))
    if not lines and type_code != RECEIPT_CODE:
        # A credit note issued with a refund has no lines; one line carries what
        # it credits, since the spec keeps that link on the lines (1256/1257).
        lines.append(_one_line(row, vat_rate, doc.credit_reason or doc.description))

    payments = []
    if type_code in PAYMENT_DOCUMENT_TYPES:
        for payment in doc.payments.all():
            payments.append(_paid(
                payment.payment_method, Decimal(payment.amount), row.document_date,
                installments=payment.card_installments or 1,
                due_date=payment.check_date,
                bank_number=_digits(payment.check_bank),
                branch_number=_digits(payment.check_branch),
                account_number=_digits(payment.check_account),
                check_number=_digits(payment.reference),
            ))

    customer = doc.business_customer if doc.business_customer_id else None
    linked = (None, '')
    if row.is_credit:
        original = doc.linked_document_number or (doc.linked_document.document_number if doc.linked_document_id else '')
        linked = _linked(original, formal_types)
    return _document(
        row, type_code, lines, payments,
        # A business customer's number only: 1215 is the customer's עוסק מורשה,
        # and a private family has none.
        customer_vat=_digits(customer.company_number or customer.id_number) if customer else '',
        linked=linked,
    )


def _lesson(row, invoice) -> UniformDocument:
    courses = sorted({link.course.name for link in invoice.children.all() if link.course_id and link.course})
    line = UniformLine(
        description='חוגים' + (f' — {", ".join(courses)}' if courses else ''),
        quantity=Decimal('1'),
        unit_price=row.net_amount,
        line_total=row.net_amount,
        vat_rate=VAT_PERCENT_DISPLAY,
    )
    payment = _paid(invoice.payment_method, row.total_amount, row.document_date)
    return _document(row, DOCUMENT_TYPE_CODES['combined'], [line], [payment])


def _store(row, invoice) -> UniformDocument:
    type_code = DOCUMENT_TYPE_CODES[row.document_type]
    on_account = row.document_type == 'transaction_invoice'
    vat_rate = ZERO if on_account else VAT_PERCENT_DISPLAY
    sales = list(invoice.line_items.all())
    lines = []
    remaining = row.net_amount
    for index, sale in enumerate(sales):
        gross = Decimal(sale.total_price)
        # The prices are with VAT in; each line is taken out of it, and the last
        # one takes the rounding, so the lines add up to the document exactly.
        if index == len(sales) - 1:
            net = remaining
        else:
            net = gross if on_account else _cents(gross / (1 + vat_rate / 100))
            remaining -= net
        quantity = Decimal(sale.quantity or 1)
        name = sale.product.name if sale.product_id and sale.product else 'פריט'
        lines.append(UniformLine(
            description=f'{name} {sale.size}'.strip() if sale.size else name,
            quantity=quantity,
            unit_price=_cents(net / quantity),
            line_total=net,
            vat_rate=vat_rate,
        ))
    if not lines:
        lines.append(_one_line(row, vat_rate, 'רכישה בחנות'))
    payments = []
    if type_code in PAYMENT_DOCUMENT_TYPES:
        payments.append(_paid(invoice.payment_method, row.total_amount, row.document_date))
    return _document(row, type_code, lines, payments)


def _documents(rows) -> list[UniformDocument]:
    from apps.customers.financial_models import Invoice
    from apps.documents.models import FormalDocument
    from apps.store.models import StoreInvoice

    def ids(channel):
        return [row.source_id for row in rows if row.channel == channel]

    formal = {
        str(doc.pk): doc for doc in FormalDocument.objects
        .filter(pk__in=ids(CHANNEL_MANUAL))
        .select_related('business_customer', 'linked_document')
        .prefetch_related('line_items', 'payments')
    }
    lessons = {
        str(invoice.pk): invoice for invoice in Invoice.objects
        .filter(pk__in=ids(CHANNEL_LESSONS))
        .prefetch_related('children__course')
    }
    store = {
        str(invoice.pk): invoice for invoice in StoreInvoice.objects
        .filter(pk__in=ids(CHANNEL_STORE))
        .prefetch_related('line_items__product')
    }
    credited = {doc.linked_document_number for doc in formal.values() if doc.linked_document_number}
    formal_types = dict(
        FormalDocument.objects.filter(document_number__in=credited).values_list('document_number', 'document_type')
    )

    out = []
    for row in rows:
        if row.channel == CHANNEL_MANUAL:
            out.append(_manual(row, formal[row.source_id], formal_types))
        elif row.channel == CHANNEL_LESSONS:
            out.append(_lesson(row, lessons[row.source_id]))
        else:
            out.append(_store(row, store[row.source_id]))
    return out


def build_uniform_export(user, start, end, label: str) -> tuple[bytes, str]:
    """(the ZIP, its file name) for the documents of a period inside one tax year."""
    if start.year != end.year:
        raise ReportInputError('המבנה האחיד מופק לשנת מס אחת — בחרו טווח בתוך אותה שנה')
    report = build_report(user, start, end, label)
    rows = [row for row in register_rows(report) if not row.void]
    foreign = sorted({row.currency for row in rows if row.currency != 'ILS'})
    if foreign:
        raise ReportInputError(
            f'בטווח יש מסמכים במטבע {", ".join(foreign)}. המבנה האחיד רושם שקלים בלבד, '
            f'ושער ההמרה של כל מסמך אינו שמור במערכת.'
        )
    generated_at = timezone.now()
    files = build_uniform_files(
        BUSINESS, _documents(rows), period_start=start, period_end=end, generated_at=generated_at,
    )
    return uniform_zip(files, BUSINESS, generated_at), f'{files.directory.replace("/", "-")}.zip'
