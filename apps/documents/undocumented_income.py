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

SOURCE_LABELS = {
    SOURCE_LESSONS: 'חיובי חוגים — נשלח מייל, לא הופק מסמך',
    SOURCE_STORE: 'מכירות חנות — הפקת המסמך בטרנזילה נכשלה',
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
    branch_name: str
    amount: Decimal


@dataclass
class SourceSection:
    source: str
    label: str
    rows: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def total(self) -> Decimal:
        return sum((row.amount for row in self.rows), Decimal('0.00'))


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
    def is_empty(self) -> bool:
        return self.count == 0


def _branch_name(branch, fallback_branch=None) -> str:
    if branch is not None:
        return branch.name
    if fallback_branch is not None:
        return fallback_branch.name
    return UNASSIGNED_BRANCH_LABEL


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
        names = [
            link.child.full_name for link in invoice.children.all()
            if link.child_id and link.child
        ]
        customer = ', '.join(names) or (invoice.payer_name or invoice.family.name)
        rows.append(UndocumentedRow(
            source=SOURCE_LESSONS,
            customer=customer,
            reference=invoice.invoice_number,
            row_date=invoice.invoice_date.date(),
            detail=_PAYMENT_TYPE_LABELS.get(invoice.payment_type, invoice.payment_type or ''),
            branch_name=_branch_name(invoice.branch, invoice.family.branch if invoice.family_id else None),
            amount=invoice.amount,
        ))
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
        rows.append(UndocumentedRow(
            source=SOURCE_STORE,
            customer=customer,
            reference=invoice.invoice_number,
            row_date=invoice.issue_date.date(),
            detail=_STORE_METHOD_LABELS.get(invoice.payment_method, invoice.payment_method or ''),
            branch_name=_branch_name(invoice.branch),
            amount=invoice.total_amount,
        ))
    return rows


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
    ):
        if rows:
            sections.append(SourceSection(source=source, label=SOURCE_LABELS[source], rows=rows))
    return UndocumentedIncome(sections=sections)
