"""The documents register — every document issued in a period, from every channel.

Four channels issue documents to customers:

* the documents module (FormalDocument): what the office issues by hand, every
  credit note, and the local copy of a store sale's Tranzila document;
* lesson charges (customers.Invoice): the חשבונית מס/קבלה each charge issues;
* the store (StoreInvoice): the חשבונית מס/קבלה of a sale paid on the spot, or the
  חשבונית עסקה of a sale put on monthly billing;
* studio rentals (apps/rental_billing): the חשבונית מס/קבלה each tenant charge
  issues. It is a FormalDocument numbered in the RT run, read with the first
  channel's documents and filed under a channel of its own.

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
    money_received_on,
)

CHANNEL_MANUAL = 'manual'
CHANNEL_LESSONS = 'lessons'
CHANNEL_STORE = 'store'
# A tenant's receipt (the RT run). period_report._row_from sets it by the number.
CHANNEL_RENTALS = 'rentals'

CHANNEL_LABELS = {
    CHANNEL_MANUAL: 'מסמכים',
    CHANNEL_LESSONS: 'חוגים',
    CHANNEL_STORE: 'חנות',
    CHANNEL_RENTALS: 'שכירויות',
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
                          'children__course__business_category', 'activity_logs')
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
            money_received_on=money_received_on(invoice),
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
        # never completed, holds its number and nothing else — a website order
        # included. A website order used to count while still pending, on the
        # site's word that it was paid; that word was never true: until 29.9.2026
        # the site paid on Tranzila's test terminal (realtest), which collected
        # nothing, and since then an order is completed only when Tranzila's own
        # report confirms the charge (payment_followup). The September register
        # listed eight such orders, ₪1,760, as issued receipts; none has a
        # charge on any of the business's terminals, and none has a signed
        # original (signing issues a store sale only once it is completed).
        void_reason = ''
        if not on_account:
            if invoice.payment_status == 'failed':
                void_reason = 'התשלום נכשל'
            elif invoice.payment_status == 'pending':
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
    'תאריך', 'מספר מסמך', 'סדרה', 'מספר הקצאה', 'סוג מסמך', 'קוד מבנה אחיד', 'מספר במבנה אחיד', 'ערוץ',
    'לקוח', 'לפני מע"מ', 'מע"מ', 'סה"כ', 'אמצעי תשלום', 'סניף', 'עסק', 'קטגוריה',
    'אסמכתא / מסמך מקושר', 'ייתכן כפל עם', 'מצב',
)

# The second section: money taken in the period with no document behind it
# (undocumented_income.py), which the period report prints beside the documents.
UNDOCUMENTED_TITLE = 'הכנסה ללא מסמך'
UNDOCUMENTED_COLUMNS = (
    'תאריך', 'מקור', 'לקוח', 'אסמכתא', 'פרטים', 'סניף', 'עסק', 'קטגוריה', 'סכום', 'מצב',
)
UNDOCUMENTED_FAILED = 'לא ניתן היה לאסוף את ההכנסה ללא מסמך — יש להפיק את הקובץ שוב'

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


def register_csv(report, *, undocumented_failed: bool = False) -> bytes:
    """
    The register as a CSV the accountant opens in Excel — one row per document.

    UTF-8 with a byte-order mark, which is how Excel knows the Hebrew is UTF-8.
    A credit note is negative, so each column sums to the period's net; a number
    that never became a document is listed with zero amounts and says why.

    Each document carries its allocation number (חשבוניות ישראל) and the number
    the uniform-structure file writes it under (uniform_export.uniform_number).
    When the report was given its income without a document
    (report.undocumented), a second section below lists it, one row a charge,
    and its total — so the file holds the period's whole take, as the report
    does. `undocumented_failed` says in the file that it could not be gathered.
    """
    from apps.documents.uniform_export import uniform_number

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(CSV_COLUMNS)
    sums = [Decimal('0.00')] * 3
    issued = 0
    for row in register_rows(report):
        if row.void:
            net = vat = total = '0.00'
            state = f'לא הפך למסמך — {row.void_reason} (₪{row.total_amount:.2f})'
        else:
            net = _signed(row.net_amount, row.is_credit)
            vat = _signed(row.vat_amount, row.is_credit)
            total = _signed(row.total_amount, row.is_credit)
            state = 'זיכוי' if row.is_credit else 'הופק'
            sums = [sums[0] + Decimal(net), sums[1] + Decimal(vat), sums[2] + Decimal(total)]
            issued += 1
        writer.writerow([
            row.document_date.strftime('%d/%m/%Y'),
            _text(row.document_number),
            series_of(row.document_number),
            row.allocation_number,
            row.document_type_label,
            UNIFORM_TYPE_CODES.get(row.document_type, ''),
            '' if row.void else _text(uniform_number(row.document_number)),
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
    # The column sums, in the columns they sum: the sheet's total is on the
    # sheet, and it is the period report's "סה"כ כולל מע"מ" (14.10.2026).
    writer.writerow(['סה"כ מסמכים', f'{issued} מסמכים', '', '', '', '', '', '', '',
                     f'{sums[0]:.2f}', f'{sums[1]:.2f}', f'{sums[2]:.2f}', '', '', '', '', '', '', ''])
    undocumented = None if undocumented_failed else getattr(report, 'undocumented', None)
    if undocumented_failed or undocumented is not None:
        _write_undocumented(writer, undocumented)
    if undocumented is not None and undocumented.refunds:
        _write_uncredited_refunds(writer, undocumented)
    _write_summary(writer, report)
    return buffer.getvalue().encode('utf-8-sig')


def _write_undocumented(writer, income) -> None:
    """The income-without-a-document section: a blank line, its title, its rows, its total."""
    writer.writerow([])
    writer.writerow([UNDOCUMENTED_TITLE])
    if income is None:
        writer.writerow([UNDOCUMENTED_FAILED])
        return
    writer.writerow(UNDOCUMENTED_COLUMNS)
    # Source by source, each with its subtotal, in the order the report reads
    # them; by date inside a source. Mixed by date, 546 lesson charges buried
    # the store's and the rentals' rows (September 2026).
    from apps.documents.undocumented_income import SOURCE_ORDER

    rank = {code: index for index, code in enumerate(SOURCE_ORDER)}
    for section in sorted(income.sections, key=lambda sec: rank.get(sec.source, len(rank))):
        for row in sorted(section.rows, key=lambda r: (r.row_date, r.reference)):
            writer.writerow([
                row.row_date.strftime('%d/%m/%Y'),
                section.label,
                _text(row.customer),
                _text(row.reference),
                _text(row.detail),
                _text(row.branch_name),
                _text(row.business_name),
                _text(row.category_name),
                f'{row.amount:.2f}',
                # Folded into a document issued by hand for the same child and sum: shown, not counted.
                f'לא נספר — נכלל במסמך {row.merged_document}' if row.merged_document else 'נספר',
            ])
        writer.writerow(['', f'סה"כ {section.label} ({section.count})', '', '', '', '', '', '',
                         f'{section.total:.2f}', ''])
    if not income.sections:
        writer.writerow(['אין הכנסה ללא מסמך בתקופה'])
    writer.writerow(['סה"כ ללא מסמך', '', '', '', '', '', '', '', f'{income.total:.2f}', ''])


UNCREDITED_REFUNDS_COLUMNS = ('תאריך', 'לקוח', 'המסמך שזוכה', 'מספר בטרנזילה', 'סניף', 'סכום')


def _write_uncredited_refunds(writer, income) -> None:
    """Card refunds no credit note covers: negative, like a credit note, and their total."""
    from apps.documents.undocumented_income import UNCREDITED_REFUNDS_LABEL

    writer.writerow([])
    writer.writerow([UNCREDITED_REFUNDS_LABEL])
    writer.writerow(UNCREDITED_REFUNDS_COLUMNS)
    for row in income.refunds:
        writer.writerow([
            row.row_date.strftime('%d/%m/%Y'),
            _text(row.customer),
            _text(row.reference),
            _text(row.detail),
            _text(row.branch_name),
            f'{-row.amount:.2f}',
        ])
    writer.writerow(['סה"כ זיכויים ללא חשבונית זיכוי', '', '', '', '', f'{-income.refunds_total:.2f}'])


def _write_summary(writer, report) -> None:
    """
    The period on one screen, at the bottom of the sheet: each document type,
    then the money in — the same lines, in the same order, as the period
    report's summary, so the two files can be checked against each other.
    """
    from apps.documents.period_report import DOCUMENT_TYPE_LABELS, TYPE_ORDER

    writer.writerow([])
    writer.writerow(['סיכום התקופה'])
    writer.writerow(['סוג מסמך', 'כמות', 'לפני מע"מ', 'מע"מ', 'סה"כ'])
    rank = {code: index for index, code in enumerate(TYPE_ORDER)}
    for code in sorted(report.type_totals, key=lambda c: rank.get(c, len(rank))):
        bucket = report.type_totals[code]
        writer.writerow([DOCUMENT_TYPE_LABELS.get(code, code), bucket.count,
                         f'{bucket.net_amount:.2f}', f'{bucket.vat_amount:.2f}', f'{bucket.total_amount:.2f}'])
    writer.writerow([])
    for label, amount in money_lines(report) + [('סה"כ כסף שנכנס בתקופה', period_take(report))]:
        writer.writerow([label, f'{amount:.2f}'])


# ---------------------------------------------------------------- the Excel file

def money_lines(report) -> list:
    """
    The period's money, line by line — the same lines in the PDF, the CSV and
    the Excel summary, so the three can be checked against each other.
    """
    lines = [('תקבולים עם מסמך (קבלות וחשבוניות מס/קבלה)', report.collected_total)]
    if report.earlier_money_total:
        lines.append(('מזה קבלות באיחור על כסף שנכנס לפני התקופה — נספר שם', -report.earlier_money_total))
    undocumented = report.undocumented
    if undocumented is not None:
        if undocumented.late_total:
            lines.append(('תקבולים שהקבלה שלהם הופקה אחרי התקופה', undocumented.late_total))
        lines.append(('תקבולים ללא מסמך', undocumented.no_document_total))
    lines.append(('זיכויים — חשבוניות זיכוי', -report.totals.credits_total))
    if report.uncredited_refunds_total:
        lines.append(('זיכויים בכרטיס ללא חשבונית זיכוי', -report.uncredited_refunds_total))
    return lines


def period_take(report):
    """What came in, net — all_income_total, or without the undocumented half when it was not gathered."""
    if report.undocumented is not None:
        return report.all_income_total
    return report.collected_total - report.earlier_money_total - report.totals.credits_total


def register_xlsx(report, *, undocumented_failed: bool = False) -> bytes:
    """
    The register as an Excel workbook the accountant can read as it opens.

    The owner asked for the file to be comfortable in Excel rather than full of
    information (14.10.2026): a summary sheet first, then the documents with
    only the columns a reader uses — right to left, header frozen, filters,
    sums in shekels and one total row — then the income without a document,
    source by source, and the refunds no credit note covers. The CSV stays as
    it was for a program that imports it; the numbers are the same report's.
    """
    from apps.documents.period_report import DOCUMENT_TYPE_LABELS, TYPE_ORDER
    from apps.documents.undocumented_income import SOURCE_ORDER
    from apps.documents.xlsx import (
        CREDIT, HEADER, MUTED, NOTE, SECTION, SUBTOTAL, TITLE, TOTAL, Sheet, workbook,
    )

    undocumented = None if undocumented_failed else getattr(report, 'undocumented', None)
    sheets = []

    # 1. The summary.
    summary = Sheet('סיכום', [46, 16, 16, 16, 18], gridlines=False)
    summary.add(['דוח מסמכים לרואה החשבון'], TITLE, merge_to=5)
    summary.add([f'קוגומלו · {report.period_label} · {report.scope_label}'.strip(' ·')], NOTE, merge_to=5)
    summary.blank()
    summary.add(['כמה כסף נכנס'], SECTION, merge_to=5)
    for label, amount in money_lines(report):
        summary.add([label, amount])
    summary.add(['סה"כ כסף שנכנס בתקופה', period_take(report)], TOTAL)
    if undocumented_failed:
        summary.add([UNDOCUMENTED_FAILED], NOTE, merge_to=5)
    summary.blank()
    summary.add(['לפי סוג מסמך'], SECTION, merge_to=5)
    summary.add(['סוג מסמך', 'כמות', 'לפני מע"מ', 'מע"מ', 'סה"כ'], HEADER)
    rank = {code: index for index, code in enumerate(TYPE_ORDER)}
    for code in sorted(report.type_totals, key=lambda c: rank.get(c, len(rank))):
        bucket = report.type_totals[code]
        summary.add([DOCUMENT_TYPE_LABELS.get(code, code), bucket.count,
                     bucket.net_amount, bucket.vat_amount, bucket.total_amount])
    entries = report.income_by_group()
    if entries:
        summary.blank()
        summary.add(['לפי סניף — כמה כסף נכנס'], SECTION, merge_to=5)
        summary.add(['סניף', 'עם מסמך', 'ללא מסמך', 'זיכויים', 'סה"כ נכנס'], HEADER)
        for entry in entries:
            summary.add([entry['title'], entry['documented'], entry['undocumented'],
                         -entry['credits'], entry['total']])
        summary.add(['סה"כ', sum(e['documented'] for e in entries), sum(e['undocumented'] for e in entries),
                     -sum(e['credits'] for e in entries), sum(e['total'] for e in entries)], TOTAL)
    summary.blank()
    summary.add(['"עם מסמך" — קבלות וחשבוניות מס/קבלה בלבד. חשבונית מס שטרם נגבתה אינה כאן, '
                 'כדי שלא ייספר אותו כסף פעמיים.'], NOTE, merge_to=5)
    sheets.append(summary)

    # 2. The documents: the columns a reader uses; the technical ones stay in the CSV.
    rows = register_rows(report)
    # A column nobody filled in this period is left out rather than shown empty.
    extra = [
        (title, attr) for title, attr in (('מספר הקצאה', 'allocation_number'),
                                          ('מסמך מקושר / אסמכתא', 'reference'),
                                          ('עסק', 'business_name'), ('קטגוריה', 'category_name'),
                                          ('ייתכן כפל עם', 'duplicate_of'))
        if any(getattr(row, attr) for row in rows)
    ]
    headers = ['תאריך', 'מספר מסמך', 'סוג מסמך', 'לקוח', 'סניף', 'לפני מע"מ', 'מע"מ', 'סה"כ',
               'אמצעי תשלום', 'מצב'] + [title for title, _ in extra]
    documents = Sheet('מסמכים', [12, 17, 18, 26, 24, 13, 12, 13, 13, 30] + [22] * len(extra))
    documents.add(headers, HEADER, header=True)
    sums = [Decimal('0.00')] * 3
    issued = 0
    for row in rows:
        sign = -1 if row.is_credit else 1
        if row.void:
            amounts = [None, None, None]
            state, look = f'לא הפך למסמך — {row.void_reason} (₪{row.total_amount:,.2f})', MUTED
        else:
            amounts = [sign * row.net_amount, sign * row.vat_amount, sign * row.total_amount]
            sums = [a + b for a, b in zip(sums, amounts)]
            issued += 1
            state, look = ('זיכוי', CREDIT) if row.is_credit else ('הופק', 'plain')
        documents.add([
            row.document_date, row.document_number, row.document_type_label, row.customer, row.branch_name,
            *amounts, PAYMENT_METHOD_LABELS.get(row.payment_method, row.payment_method), state,
        ] + [getattr(row, attr) for _, attr in extra], look)
    # A blank row first: Excel's filter and sort stop there, so the total is never sorted in among the rows.
    documents.blank()
    documents.add(['סה"כ', f'{issued} מסמכים', '', '', '', *sums], TOTAL)
    sheets.append(documents)

    # 3. Income with no document, source by source.
    if undocumented is not None and undocumented.sections:
        sheet = Sheet('ללא מסמך', [12, 26, 30, 16, 24, 14, 26], filterable=False)
        sheet.add(['תאריך', 'לקוח', 'אסמכתא', 'פירוט', 'סניף', 'סכום', 'מצב'], HEADER, header=True)
        order = {code: index for index, code in enumerate(SOURCE_ORDER)}
        for section in sorted(undocumented.sections, key=lambda sec: order.get(sec.source, len(order))):
            sheet.add([section.label], SECTION, merge_to=7)
            for row in sorted(section.rows, key=lambda r: (r.row_date, r.reference)):
                merged = row.merged_document
                sheet.add([row.row_date, row.customer, row.reference, row.detail, row.branch_name, row.amount,
                           f'לא נספר — נכלל במסמך {merged}' if merged else ''], MUTED if merged else 'plain')
            sheet.add([f'סה"כ — {section.count} חיובים', '', '', '', '', section.total], SUBTOTAL)
        sheet.add(['סה"כ ללא מסמך', '', '', '', '', undocumented.total], TOTAL)
        sheets.append(sheet)

    # 4. Refunds no credit note covers.
    if undocumented is not None and undocumented.refunds:
        sheet = Sheet('זיכויים ללא חשבונית', [12, 26, 34, 16, 24, 14])
        sheet.add(['תאריך', 'לקוח', 'המסמך שזוכה', 'מספר בטרנזילה', 'סניף', 'סכום'], HEADER, header=True)
        for row in undocumented.refunds:
            sheet.add([row.row_date, row.customer, row.reference, row.detail, row.branch_name, -row.amount], CREDIT)
        sheet.blank()
        sheet.add(['סה"כ', f'{len(undocumented.refunds)} זיכויים', '', '', '', -undocumented.refunds_total], TOTAL)
        sheets.append(sheet)

    return workbook(sheets)
