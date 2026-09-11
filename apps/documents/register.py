"""The documents register — every document issued in a period, from every channel.

Three channels issue documents to customers:

* the documents module (FormalDocument): what the office issues by hand, every
  credit note, and the local copy of a store sale's Tranzila document;
* lesson charges (customers.Invoice): the חשבונית מס/קבלה each charge issues;
* the store (StoreInvoice): the חשבונית מס/קבלה of a sale paid on the spot, or the
  חשבונית עסקה of a sale put on monthly billing.

The period report used to read the first channel only and list the other two as
income without a document, because they were numbered from a payment's UUID
('INV-…'): no fiscal number, no run to check. They number from consecutive runs
now (numbering.py: IR, ST, SD), so a receipt or a sale carrying such a number is
a document here like any other. One with an old number is still what it was, and
the report still lists it as income without a document — a closed month reads
the same as it always did.

A store sale that has a Tranzila copy is listed once, by that copy, as the report
always did; the copy's reference is the sale's own number.

Read-only, like the report: every amount is a stored column. A lesson receipt and
a store sale store only their gross total, so the VAT split is the one their own
PDF prints (apps.core.vat.split_vat_inclusive).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.core.vat import split_vat_inclusive
from apps.documents.numbering import LESSON_RUN_REGEX, STORE_RUN_REGEX
from apps.documents.period_report import DOCUMENT_TYPE_LABELS, ReportRow
from apps.documents.undocumented_income import (
    _branch_of,
    _card_link_tags,
    _delivery_tags,
    _income_tags,
)

CHANNEL_MANUAL = 'manual'
CHANNEL_LESSONS = 'lessons'
CHANNEL_STORE = 'store'

CHANNEL_LABELS = {
    CHANNEL_MANUAL: 'מסמכים',
    CHANNEL_LESSONS: 'חוגים',
    CHANNEL_STORE: 'חנות',
}

# מבנה אחיד, נספח 1: the code each document type is reported under.
UNIFORM_TYPE_CODES = {
    'transaction_invoice': 300,
    'tax_invoice': 305,
    'combined': 320,
    'credit_invoice': 330,
    'receipt': 400,
}

PAYMENT_METHOD_LABELS = {
    'credit_card': 'אשראי',
    'cash': 'מזומן',
    'check': "צ'ק",
    'bank_transfer': 'העברה בנקאית',
    'monthly_billing': 'חיוב חודשי',
}

ZERO = Decimal('0.00')


@dataclass
class ChannelDocument:
    """
    A lesson receipt or a store sale as a report row, with what files it into a group.

    The attribute names are the ones undocumented_income._group_key reads, so this
    money is grouped by exactly the rule it was grouped by when it was listed as
    income without a document: the same key lands on the same line of the report.
    """
    row: ReportRow
    branch_id: object = None
    branch_name: str = ''
    business_id: object = None
    business_name: str = ''
    category_id: object = None
    category_name: str = ''
    is_delivery: bool = False
    # Whose money it was — used only to point at a document issued twice.
    child_ids: tuple = ()


def lesson_documents(branch_ids, start, end, document_type: str = '') -> list[ChannelDocument]:
    """Every lesson receipt numbered in the IR run and dated in the period."""
    from apps.customers.financial_models import Invoice

    if document_type and document_type != 'combined':
        return []
    qs = (
        Invoice.objects
        .filter(invoice_date__date__gte=start, invoice_date__date__lte=end,
                invoice_number__regex=LESSON_RUN_REGEX)
        .select_related('family', 'family__branch', 'branch',
                        'payment__card_link__business', 'payment__card_link__business_category')
        .prefetch_related('children__child', 'children__course__business',
                          'children__course__business_category')
        .order_by('invoice_date', 'invoice_number')
    )
    if branch_ids is not None:
        # The partner rule undocumented_income applies to these same receipts.
        qs = qs.filter(Q(branch_id__in=branch_ids)
                       | Q(branch__isnull=True, family__branch_id__in=branch_ids))

    out = []
    for invoice in qs:
        links = list(invoice.children.all())
        names = [link.child.full_name for link in links if link.child_id and link.child]
        family = invoice.family if invoice.family_id else None
        customer = ', '.join(names) or invoice.payer_name or (family.name if family else '') or 'ללא שם לקוח'
        branch_id, branch_name = _branch_of(invoice.branch, family.branch if family else None)
        course = next((link.course for link in links if link.course_id), None)
        tags = _income_tags(course) or _card_link_tags(invoice)
        net, vat, gross = split_vat_inclusive(invoice.amount)
        # A receipt is issued with the charge that paid it; one whose charge
        # never went through was never a receipt, whatever number it holds.
        void = invoice.status in ('pending', 'failed')
        row = ReportRow(
            customer=customer,
            document_number=invoice.invoice_number,
            document_type='combined',
            document_type_label=DOCUMENT_TYPE_LABELS['combined'],
            document_date=timezone.localtime(invoice.invoice_date).date(),
            subtotal=net, discount_amount=ZERO, net_amount=net, vat_amount=vat, total_amount=gross,
            is_credit=False, currency='ILS', vat_exempt=False,
            channel=CHANNEL_LESSONS,
            source_id=str(invoice.id),
            payment_method=invoice.payment_method or '',
            branch_name=branch_name if branch_id is not None else '',
            business_name=tags.get('business_name', ''),
            category_name=tags.get('category_name', ''),
            void=void,
            void_reason='החיוב לא הושלם' if void else '',
        )
        out.append(ChannelDocument(
            row=row, branch_id=branch_id, branch_name=branch_name,
            child_ids=tuple(link.child_id for link in links if link.child_id), **tags,
        ))
    return out


def store_documents(branch_ids, start, end, document_type: str = '') -> list[ChannelDocument]:
    """Every store sale numbered in the ST or SD run and dated in the period, unless a Tranzila copy lists it."""
    from apps.store.models import StoreInvoice

    qs = (
        StoreInvoice.objects
        .filter(issue_date__date__gte=start, issue_date__date__lte=end,
                invoice_number__regex=STORE_RUN_REGEX, formal_document__isnull=True)
        .select_related('branch', 'child')
        .order_by('issue_date', 'invoice_number')
    )
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)

    delivery = None
    out = []
    for invoice in qs:
        on_account = invoice.invoice_number.startswith('SD-')
        doc_type = 'transaction_invoice' if on_account else 'combined'
        if document_type and document_type != doc_type:
            continue
        if on_account:
            # A חשבונית עסקה is a demand for payment, not a tax document, so no
            # VAT is reported on it — the tax document issued on payment carries it.
            net, vat, gross = invoice.total_amount, ZERO, invoice.total_amount
        else:
            net, vat, gross = split_vat_inclusive(invoice.total_amount)
        # A receipt says the money came in. A sale whose payment failed, or was
        # never completed at the counter, holds its number and nothing else. A
        # confirmed website order counts, as it does on the dashboard.
        void_reason = ''
        if not on_account:
            if invoice.payment_status == 'failed':
                void_reason = 'התשלום נכשל'
            elif invoice.payment_status == 'pending' and not invoice.website_order_number:
                void_reason = 'התשלום טרם הושלם'
        customer = (
            (invoice.child.full_name if invoice.child_id and invoice.child else '')
            or invoice.customer_name
            or 'לקוח מזדמן'
        )
        branch_id, branch_name = _branch_of(invoice.branch)
        tags = {}
        if invoice.branch_id is None:
            # A sale with no branch is a website delivery: the brand's, as on the dashboard.
            if delivery is None:
                delivery = _delivery_tags()
            tags = delivery
        row = ReportRow(
            customer=customer,
            document_number=invoice.invoice_number,
            document_type=doc_type,
            document_type_label=DOCUMENT_TYPE_LABELS[doc_type],
            document_date=timezone.localtime(invoice.issue_date).date(),
            subtotal=net, discount_amount=ZERO, net_amount=net, vat_amount=vat, total_amount=gross,
            is_credit=False, currency='ILS', vat_exempt=on_account,
            channel=CHANNEL_STORE,
            source_id=str(invoice.id),
            reference=invoice.website_order_number or '',
            payment_method=invoice.payment_method or '',
            branch_name=branch_name if branch_id is not None else '',
            business_name=tags.get('business_name', ''),
            category_name=tags.get('category_name', ''),
            void=bool(void_reason),
            void_reason=void_reason,
        )
        out.append(ChannelDocument(
            row=row, branch_id=branch_id, branch_name=branch_name,
            child_ids=(invoice.child_id,) if invoice.child_id else (), **tags,
        ))
    return out


def channel_documents(branch_ids, start, end, document_type: str = '') -> list[ChannelDocument]:
    """The lesson receipts and the store sales of the period that are documents."""
    return (
        lesson_documents(branch_ids, start, end, document_type)
        + store_documents(branch_ids, start, end, document_type)
    )


def find_possible_duplicates(channel: list[ChannelDocument], documents) -> list[dict]:
    """
    A lesson receipt and a document issued by hand for the same child and sum.

    Before lesson charges issued documents of their own, the office covered some
    of them by hand, so one payment may now carry both: two documents for one
    sum. Nothing links a charge to a document, so this matches the way
    undocumented_income.merge_against_documents does — same child, same sum, same
    period — and only points at the pair. Which one stands is the accountant's call.
    """
    index: dict = {}
    for doc in documents:
        if doc.document_type in ('credit_invoice', 'draft') or not doc.child_id:
            continue
        if any(True for _ in doc.store_invoices.all()):
            continue  # a store sale's Tranzila copy, not an office document
        index.setdefault((doc.child_id, Decimal(doc.total_amount)), []).append(doc.document_number)

    pairs = []
    for item in channel:
        if item.row.void or item.row.channel != CHANNEL_LESSONS:
            continue
        for child_id in item.child_ids:
            numbers = index.get((child_id, item.row.total_amount))
            if numbers:
                pairs.append({
                    'number': item.row.document_number,
                    'other': numbers.pop(0),
                    'customer': item.row.customer,
                    'amount': item.row.total_amount,
                    'date': item.row.document_date,
                })
                break
    return pairs


# ---------------------------------------------------------------- the export

CSV_COLUMNS = (
    'תאריך', 'מספר מסמך', 'סדרה', 'סוג מסמך', 'קוד מבנה אחיד', 'ערוץ', 'לקוח',
    'לפני מע"מ', 'מע"מ', 'סה"כ', 'אמצעי תשלום', 'סניף', 'עסק', 'קטגוריה',
    'אסמכתא / מסמך מקושר', 'ייתכן כפל עם', 'מצב',
)

_RUN_NUMBER = re.compile(r'^([A-Z]+)-\d{4}-\d+$')
_SHARED_RUN_NUMBER = re.compile(r'^\d{4}-\d{4,}$')


def series_of(number: str) -> str:
    """The run a number belongs to: its prefix, the closed shared run, or '' (a Tranzila number)."""
    match = _RUN_NUMBER.match(number or '')
    if match:
        return match.group(1)
    if _SHARED_RUN_NUMBER.match(number or ''):
        return 'משותפת (סגורה)'
    return ''


def register_rows(report) -> list[ReportRow]:
    """Every row of a report, and every number that never became a document, by date and number."""
    rows = [row for group in report.groups for row in group.rows] + list(report.void_rows)
    return sorted(rows, key=lambda row: (row.document_date, row.document_number))


def _text(value) -> str:
    """A cell Excel keeps as text: one that starts like a formula is quoted."""
    text = str(value or '')
    return f"'{text}" if text[:1] in ('=', '+', '-', '@') else text


def _signed(amount: Decimal, negative: bool) -> str:
    return f'{-amount if negative else amount:.2f}'


def register_csv(report) -> bytes:
    """
    The register as a CSV the accountant opens in Excel — one row per document.

    UTF-8 with a byte-order mark, which is how Excel knows the Hebrew is UTF-8.
    A credit note is negative, so each column sums to the period's net; a number
    that never became a document is listed with zero amounts and says why.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(CSV_COLUMNS)
    for row in register_rows(report):
        if row.void:
            net = vat = total = '0.00'
            state = f'לא הפך למסמך — {row.void_reason} (₪{row.total_amount:.2f})'
        else:
            net = _signed(row.net_amount, row.is_credit)
            vat = _signed(row.vat_amount, row.is_credit)
            total = _signed(row.total_amount, row.is_credit)
            state = 'זיכוי' if row.is_credit else 'הופק'
        writer.writerow([
            row.document_date.strftime('%d/%m/%Y'),
            _text(row.document_number),
            series_of(row.document_number),
            row.document_type_label,
            UNIFORM_TYPE_CODES.get(row.document_type, ''),
            CHANNEL_LABELS.get(row.channel, row.channel),
            _text(row.customer),
            net,
            vat,
            total,
            PAYMENT_METHOD_LABELS.get(row.payment_method, row.payment_method),
            _text(row.branch_name),
            _text(row.business_name),
            _text(row.category_name),
            _text(row.reference),
            _text(row.duplicate_of),
            state,
        ])
    return buffer.getvalue().encode('utf-8-sig')
