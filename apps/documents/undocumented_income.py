"""Money taken in a period that produced no formal document.

The period report counts FormalDocument rows, which is what an accountant asks
for. It is not the whole of what the business took in, because only two flows
issue a document at all: manual issuance from the invoices page (including the
check-plan cron) and a store order whose Tranzila document succeeded.

A lesson charge issues none. It creates a customers.Invoice, mails the payer a
PDF rendered here rather than by Tranzila, and stops — so the money is real and
the document is not. A store order whose Tranzila call failed leaves the same
shape: a StoreInvoice with formal_document NULL.

Neither can be printed beside the documents: they carry no fiscal number and
summing them into the document totals would misstate both. They are collected
here as their own section so the report can state the period's whole take and
say plainly which part of it has no document behind it.

Read-only by construction, like period_report: every figure is a stored column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q

from apps.core.scoping import is_scoped_partner, partner_branch_ids

SOURCE_LESSONS = 'lessons'
SOURCE_STORE = 'store'
SOURCE_ORPHAN_CHARGES = 'orphan_charges'

SOURCE_LABELS = {
    SOURCE_LESSONS: 'חיובי חוגים — נשלח מייל, לא הופק מסמך',
    SOURCE_STORE: 'מכירות חנות — הפקת המסמך בטרנזילה נכשלה',
    SOURCE_ORPHAN_CHARGES: 'חיובים שנגבו ואין להם אפילו רשומת חשבונית',
}

# Same predicate the dashboard uses for store revenue, so the two never disagree:
# completed charges plus confirmed website orders whose stock was already taken.
_STORE_PAID = Q(payment_status='completed') | Q(
    website_order_number__isnull=False, payment_status='pending',
)

_PAYMENT_TYPE_LABELS = {
    'recurring': 'מנוי חוזר',
    'one_time': 'חד-פעמי',
    'manual': 'ידני',
}

# Payment names the same two things differently from Invoice.
_CHARGE_TYPE_LABELS = {
    'recurring_subscription': 'מנוי חוזר',
    'one_time': 'חד-פעמי',
}

_STORE_METHOD_LABELS = {
    'credit_card': 'אשראי',
    'cash': 'מזומן',
    'monthly_billing': 'הוראת קבע',
}

UNASSIGNED_BRANCH_LABEL = 'ללא שיוך לסניף'


@dataclass
class UndocumentedRow:
    source: str
    customer: str
    reference: str
    row_date: date
    detail: str
    branch_id: object
    branch_name: str
    amount: Decimal
    # Whose money this was, used only to find an issued document for the same
    # sum. Never printed.
    child_ids: list = field(default_factory=list)
    # Set when such a document was found. The row stays visible — the owner
    # asked to see what was merged — but stops counting.
    merged_document: str = ''


@dataclass
class SourceSection:
    source: str
    label: str
    rows: list = field(default_factory=list)

    @property
    def counted(self) -> list:
        return [row for row in self.rows if not row.merged_document]

    @property
    def merged(self) -> list:
        return [row for row in self.rows if row.merged_document]

    @property
    def count(self) -> int:
        return len(self.counted)

    @property
    def total(self) -> Decimal:
        return sum((row.amount for row in self.counted), Decimal('0.00'))

    @property
    def merged_total(self) -> Decimal:
        return sum((row.amount for row in self.merged), Decimal('0.00'))


@dataclass
class UndocumentedIncome:
    sections: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(section.count for section in self.sections)

    @property
    def total(self) -> Decimal:
        return sum((section.total for section in self.sections), Decimal('0.00'))

    @property
    def merged_count(self) -> int:
        return sum(len(section.merged) for section in self.sections)

    @property
    def merged_total(self) -> Decimal:
        return sum((section.merged_total for section in self.sections), Decimal('0.00'))

    @property
    def is_empty(self) -> bool:
        return self.count == 0 and self.merged_count == 0

    def by_branch(self) -> dict:
        """branch key -> (name, amount), counting only what was not merged."""
        out: dict = {}
        for section in self.sections:
            for row in section.counted:
                # Same key the report groups branches by, so a branch with both
                # kinds of income lands on one line rather than two.
                key = row.branch_id if row.branch_id is not None else row.branch_name
                name, amount = out.get(key, (row.branch_name, Decimal('0.00')))
                out[key] = (name, amount + row.amount)
        return out


def _branch_of(branch, fallback_branch=None) -> tuple:
    """(id, name) for the row, falling back to the family's branch."""
    if branch is not None:
        return branch.id, branch.name
    if fallback_branch is not None:
        return fallback_branch.id, fallback_branch.name
    return None, UNASSIGNED_BRANCH_LABEL


def _lesson_rows(branch_ids, start: date, end: date) -> list:
    from apps.customers.financial_models import Invoice

    qs = (
        Invoice.objects
        .filter(status='paid', invoice_date__date__gte=start, invoice_date__date__lte=end)
        .select_related('family', 'family__branch', 'branch')
        .prefetch_related('children__child')
        .order_by('invoice_date', 'invoice_number')
    )
    if branch_ids is not None:
        # A partner sees a charge through the invoice's branch, or the family's
        # when the charge was written without one.
        qs = qs.filter(Q(branch_id__in=branch_ids)
                       | Q(branch__isnull=True, family__branch_id__in=branch_ids))

    rows = []
    for invoice in qs:
        links = list(invoice.children.all())
        names = [link.child.full_name for link in links if link.child_id and link.child]
        customer = ', '.join(names) or (invoice.payer_name or invoice.family.name)
        branch_id, branch_name = _branch_of(
            invoice.branch, invoice.family.branch if invoice.family_id else None,
        )
        row = UndocumentedRow(
            source=SOURCE_LESSONS,
            customer=customer,
            reference=invoice.invoice_number,
            row_date=invoice.invoice_date.date(),
            detail=_PAYMENT_TYPE_LABELS.get(invoice.payment_type, invoice.payment_type or ''),
            branch_id=branch_id,
            branch_name=branch_name,
            amount=invoice.amount,
            child_ids=[link.child_id for link in links if link.child_id],
        )
        rows.append(row)
    return rows


def _store_rows(branch_ids, start: date, end: date) -> list:
    from apps.store.models import StoreInvoice

    qs = (
        StoreInvoice.objects
        .filter(_STORE_PAID, formal_document__isnull=True,
                issue_date__date__gte=start, issue_date__date__lte=end)
        .select_related('branch', 'child', 'child__family', 'child__family__branch')
        .order_by('issue_date', 'invoice_number')
    )
    if branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)

    rows = []
    for invoice in qs:
        customer = (
            (invoice.child.full_name if invoice.child_id and invoice.child else '')
            or invoice.customer_name
            or 'לקוח מזדמן'
        )
        branch_id, branch_name = _branch_of(invoice.branch)
        rows.append(UndocumentedRow(
            source=SOURCE_STORE,
            customer=customer,
            reference=invoice.invoice_number,
            row_date=invoice.issue_date.date(),
            detail=_STORE_METHOD_LABELS.get(invoice.payment_method, invoice.payment_method or ''),
            branch_id=branch_id,
            branch_name=branch_name,
            amount=invoice.total_amount,
        ))
    return rows


def _orphan_charge_rows(branch_ids, start: date, end: date) -> list:
    """
    Completed charges that never got even an Invoice row.

    Writing the Invoice is wrapped in try/except at every call site, so a
    charge can succeed on Tranzila and leave nothing behind but the Payment.
    Those shekels are in the bank either way, and a report of everything that
    came in cannot be the one place they are missing.
    """
    from apps.customers.models import Payment

    dated = (
        Q(payment_date__date__gte=start, payment_date__date__lte=end)
        | Q(payment_date__isnull=True, created_at__date__gte=start, created_at__date__lte=end)
    )
    qs = (
        Payment.objects
        .filter(dated, status='completed', invoices__isnull=True, final_amount__gt=0)
        .select_related('child', 'family', 'family__branch', 'branch')
        .order_by('payment_date', 'created_at')
    )
    if branch_ids is not None:
        qs = qs.filter(Q(branch_id__in=branch_ids)
                       | Q(branch__isnull=True, family__branch_id__in=branch_ids))

    rows = []
    for payment in qs:
        when = payment.payment_date or payment.created_at
        customer = (
            (payment.child.full_name if payment.child_id and payment.child else '')
            or (payment.family.name if payment.family_id else '')
            or 'ללא שם'
        )
        branch_id, branch_name = _branch_of(
            payment.branch, payment.family.branch if payment.family_id else None,
        )
        row = UndocumentedRow(
            source=SOURCE_ORPHAN_CHARGES,
            customer=customer,
            reference=str(payment.id)[:8].upper(),
            row_date=when.date(),
            detail=_CHARGE_TYPE_LABELS.get(payment.payment_type, payment.payment_type or ''),
            branch_id=branch_id,
            branch_name=branch_name,
            amount=payment.final_amount,
            child_ids=[payment.child_id] if payment.child_id else [],
        )
        rows.append(row)
    return rows


def _issued_document_index(start: date, end: date) -> dict:
    """
    (child, amount) -> the numbers of documents issued for that child and sum.

    Only documents raised for a registered child can be matched at all, and a
    credit is money going the other way, so neither it nor a document already
    tied to a store invoice belongs in the index.
    """
    from apps.documents.models import FormalDocument

    rows = (
        FormalDocument.objects
        .filter(document_date__gte=start, document_date__lte=end, child__isnull=False)
        .exclude(document_type__in=('draft', 'credit_invoice'))
        .filter(store_invoices__isnull=True)
        .values_list('child_id', 'total_amount', 'document_number')
        .order_by('document_date')
    )
    index: dict = {}
    for child_id, total, number in rows:
        index.setdefault((child_id, Decimal(total)), []).append(number)
    return index


def merge_against_documents(income: UndocumentedIncome, start: date, end: date) -> None:
    """
    Fold a charge into a document that was issued by hand for the same money.

    There is no field linking a charge to a document, so the match is made on
    what the two have in common: the same child, the same sum, the same period.
    A document is consumed by the first charge that matches it, so two equal
    charges never both hide behind one receipt.

    Deliberately narrow. A wrong merge hides money the owner took in, which is
    worse than showing a duplicate the merge note points at, and this is a
    stopgap until a charge issues its own document and the guessing stops.
    """
    index = _issued_document_index(start, end)
    if not index:
        return
    for section in income.sections:
        for row in section.rows:
            for child_id in row.child_ids:
                numbers = index.get((child_id, row.amount))
                if numbers:
                    row.merged_document = numbers.pop(0)
                    break


def collect_undocumented(user, start: date, end: date) -> UndocumentedIncome:
    """
    Paid income in the period with no FormalDocument behind it.

    Scoped like the documents themselves: a partner sees only their branches,
    and a partner with no branch assigned sees nothing rather than everything.
    """
    branch_ids = None
    if is_scoped_partner(user):
        branch_ids = partner_branch_ids(user) or []
        if not branch_ids:
            return UndocumentedIncome(sections=[])

    sections = []
    for source, rows in (
        (SOURCE_LESSONS, _lesson_rows(branch_ids, start, end)),
        (SOURCE_STORE, _store_rows(branch_ids, start, end)),
        (SOURCE_ORPHAN_CHARGES, _orphan_charge_rows(branch_ids, start, end)),
    ):
        if rows:
            sections.append(SourceSection(source=source, label=SOURCE_LABELS[source], rows=rows))

    income = UndocumentedIncome(sections=sections)
    merge_against_documents(income, start, end)
    return income
