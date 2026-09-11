"""Money taken in a period that produced no document.

The period report lists every document (register.py): what the office issues,
and the lesson receipts and store sales numbered in consecutive runs (IR, ST,
SD). What is left is collected here, so the report can state the period's whole
take and say plainly which part of it has no document behind it:

* a lesson charge or a store sale from before those runs existed. It carries an
  'INV-…' number cut from a payment's UUID — no fiscal number — so summing it
  into the document totals would misstate both. Such a store sale that got a
  Tranzila document is listed by that document instead;
* a charge that went through and never got even an Invoice row;
* a payment through a payment link that issued no document.

Read-only by construction, like period_report: every figure is a stored column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q

from apps.core.revenue_service import (
    BRANCHES_BUSINESS_KEY,
    BRANCHES_BUSINESS_LABEL,
    DELIVERY_BUSINESS_LABEL,
    DELIVERY_CATEGORY_LABEL,
)
from apps.core.scoping import is_scoped_partner, partner_branch_ids
from apps.documents.numbering import LESSON_RUN_REGEX, STORE_RUN_REGEX
from apps.documents.period_report import (
    GROUP_BY_BRANCH,
    GROUP_BY_CATEGORY,
    GROUP_BY_UNIT,
    PRIVATE_CUSTOMERS_LABEL,
    UNTAGGED_BUSINESS_LABEL,
    UNTAGGED_CATEGORY_LABEL,
)

SOURCE_LESSONS = 'lessons'
SOURCE_STORE = 'store'
SOURCE_ORPHAN_CHARGES = 'orphan_charges'
SOURCE_PAYMENT_LINKS = 'payment_links'

SOURCE_LABELS = {
    SOURCE_LESSONS: 'חיובי חוגים במספור הישן — נשלח מייל, לא מסמך במספר עוקב',
    SOURCE_STORE: 'מכירות חנות במספור הישן — ללא מסמך טרנזילה',
    SOURCE_ORPHAN_CHARGES: 'חיובים שנגבו ואין להם אפילו רשומת חשבונית',
    SOURCE_PAYMENT_LINKS: 'תשלומים בקישור — לא הופק מסמך',
}
# The order the sources read in inside a group: the regular thing first, the
# store next, the oddity last.
SOURCE_ORDER = (SOURCE_LESSONS, SOURCE_STORE, SOURCE_ORPHAN_CHARGES, SOURCE_PAYMENT_LINKS)

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
DELIVERY_KEY = 'delivery'
DELIVERY_TITLE = f'{DELIVERY_BUSINESS_LABEL} · {DELIVERY_CATEGORY_LABEL}'

# Reading order inside a grouping: the branches, then the brand's deliveries,
# then whatever could not be placed.
RANK_NAMED, RANK_DELIVERY, RANK_UNASSIGNED = 0, 1, 2


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
    # Income tags, read off the course the charge was for. A store sale has
    # none, and a charge whose course was never tagged has none either.
    business_id: object = None
    business_name: str = ''
    category_id: object = None
    category_name: str = ''
    # A website order shipped to the customer. It has no branch by design,
    # and the owner files it under the brand rather than under "unassigned".
    is_delivery: bool = False
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
class UndocumentedGroup:
    """One branch (or business, or category) worth of charges, with its subtotal."""
    key: object
    title: str
    rows: list = field(default_factory=list)
    is_unassigned: bool = False
    rank: int = RANK_NAMED

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

    @property
    def sections(self) -> list:
        """Rows split by source, in reading order, each with its own subtotal."""
        by_source: dict = {}
        for row in self.rows:
            section = by_source.get(row.source)
            if section is None:
                section = SourceSection(row.source, SOURCE_LABELS[row.source])
                by_source[row.source] = section
            section.rows.append(row)
        rank = {code: index for index, code in enumerate(SOURCE_ORDER)}
        return sorted(by_source.values(), key=lambda sec: rank.get(sec.source, len(rank)))


def _group_key(row: UndocumentedRow, group_by: str) -> tuple:
    """
    (key, title, rank) — the bucket the owner files this money under.

    The rule is the one the dashboard already follows: a charge or a pickup
    sale belongs to its branch, so under a business grouping the branches are
    one business with a category per branch; a website delivery belongs to
    the brand; a course carrying its own tags goes under those. The keys match
    the document side where a real row exists, so a tagged document and a
    charge for the same business land on one line in the closing table.
    """
    if group_by == GROUP_BY_UNIT:
        if row.business_id is not None:
            return row.business_id, row.business_name, RANK_DELIVERY if row.is_delivery else RANK_NAMED
        if row.branch_id is not None:
            return BRANCHES_BUSINESS_KEY, BRANCHES_BUSINESS_LABEL, RANK_NAMED
        return UNTAGGED_BUSINESS_LABEL, UNTAGGED_BUSINESS_LABEL, RANK_UNASSIGNED
    if group_by == GROUP_BY_CATEGORY:
        if row.category_id is not None:
            return (row.category_id, f'{row.business_name} · {row.category_name}',
                    RANK_DELIVERY if row.is_delivery else RANK_NAMED)
        if row.branch_id is not None:
            return (f'{BRANCHES_BUSINESS_KEY}:{row.branch_id}',
                    f'{BRANCHES_BUSINESS_LABEL} · {row.branch_name}', RANK_NAMED)
        return UNTAGGED_CATEGORY_LABEL, UNTAGGED_CATEGORY_LABEL, RANK_UNASSIGNED
    if group_by == GROUP_BY_BRANCH:
        if row.branch_id is not None:
            return row.branch_id, row.branch_name, RANK_NAMED
        if row.is_delivery:
            return DELIVERY_KEY, DELIVERY_TITLE, RANK_DELIVERY
        return UNASSIGNED_BRANCH_LABEL, UNASSIGNED_BRANCH_LABEL, RANK_UNASSIGNED
    # Grouping by business customer: a charge is never one, so they all sit in
    # the same private bucket the document side uses.
    return PRIVATE_CUSTOMERS_LABEL, PRIVATE_CUSTOMERS_LABEL, RANK_UNASSIGNED


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

    def grouped(self, group_by: str = GROUP_BY_BRANCH) -> list:
        """
        Every row filed under exactly one group, named groups first in
        alphabetical order and the catch-all bucket last — the order the
        document side of the report uses, so the two read the same way.
        """
        groups: dict = {}
        for section in self.sections:
            for row in section.rows:
                key, title, rank = _group_key(row, group_by)
                group = groups.get(key)
                if group is None:
                    group = UndocumentedGroup(key=key, title=title, rank=rank,
                                              is_unassigned=rank == RANK_UNASSIGNED)
                    groups[key] = group
                group.rows.append(row)
        return sorted(groups.values(), key=lambda g: (g.rank, g.title))

    def by_group(self, group_by: str = GROUP_BY_BRANCH) -> dict:
        """group key -> (title, amount), counting only what was not merged."""
        return {group.key: (group.title, group.total) for group in self.grouped(group_by)}

    def by_branch(self) -> dict:
        return self.by_group(GROUP_BY_BRANCH)


def _income_tags(course) -> dict:
    """The business and category a course is tagged with, or nothing."""
    if course is None:
        return {}
    tags = {}
    if course.business_id:
        tags['business_id'] = course.business_id
        tags['business_name'] = course.business.name
    if course.business_category_id:
        tags['category_id'] = course.business_category_id
        tags['category_name'] = course.business_category.name
    return tags


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
        # A receipt numbered in the IR run is a document, listed with the rest (register.py).
        .exclude(invoice_number__regex=LESSON_RUN_REGEX)
        .select_related('family', 'family__branch', 'branch',
                        'payment__card_link__business', 'payment__card_link__business_category')
        .prefetch_related('children__child', 'children__course__business',
                          'children__course__business_category')
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
        # One charge may cover two children in two courses; the first course
        # that carries a tag names the business, which is how the invoice
        # page tags a document for the same family.
        course = next((link.course for link in links if link.course_id), None)
        tags = _income_tags(course)
        if not tags:
            tags = _card_link_tags(invoice)
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
            **tags,
        )
        rows.append(row)
    return rows


def _card_link_tags(invoice) -> dict:
    """A one-time charge through a card link carries the link's tags, not a course's."""
    payment = getattr(invoice, 'payment', None)
    link = getattr(payment, 'card_link', None) if payment is not None else None
    if link is None:
        return {}
    tags = {}
    if link.business_id:
        tags['business_id'] = link.business_id
        tags['business_name'] = link.business.name
    if link.business_category_id:
        tags['category_id'] = link.business_category_id
        tags['category_name'] = link.business_category.name
    return tags


def _store_rows(branch_ids, start: date, end: date, delivery: dict) -> list:
    from apps.store.models import StoreInvoice

    qs = (
        StoreInvoice.objects
        .filter(_STORE_PAID, formal_document__isnull=True,
                issue_date__date__gte=start, issue_date__date__lte=end)
        # A sale numbered in the ST or SD run is a document, listed with the rest.
        .exclude(invoice_number__regex=STORE_RUN_REGEX)
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
            **(delivery if invoice.branch_id is None else {}),
        ))
    return rows


def _delivery_tags() -> dict:
    """
    The brand and its deliveries category, as real rows when they exist.

    Both are seeded by migration, so normally they do; the string keys are a
    fallback that still groups deliveries together on a database that has not
    run it yet, rather than scattering them into "unassigned".
    """
    from apps.core.models import Business

    tags = {
        'is_delivery': True,
        'business_id': DELIVERY_KEY, 'business_name': DELIVERY_BUSINESS_LABEL,
        'category_id': DELIVERY_KEY, 'category_name': DELIVERY_CATEGORY_LABEL,
    }
    business = Business.objects.filter(name=DELIVERY_BUSINESS_LABEL).first()
    if business is None:
        return tags
    tags['business_id'] = business.id
    category = business.categories.filter(name=DELIVERY_CATEGORY_LABEL).first()
    if category is not None:
        tags['category_id'] = category.id
    return tags


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
        .select_related('child', 'family', 'family__branch', 'branch',
                        'lesson__course__business', 'lesson__course__business_category')
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
            **_income_tags(payment.lesson.course if payment.lesson_id else None),
        )
        rows.append(row)
    return rows


def _payment_link_rows(branch_ids, start: date, end: date) -> list:
    """Money paid through a payment link with no document behind it (child_ids empty: nothing merges by guess)."""
    from apps.payment_links.finance import completed_link_payments

    rows = []
    qs = completed_link_payments(start, end, branch_ids=branch_ids).filter(formal_document__isnull=True)
    for payment in qs:
        link = payment.link
        branch_id, branch_name = _branch_of(link.branch)
        tags = {}
        if link.business_id:
            tags['business_id'] = link.business_id
            tags['business_name'] = link.business.name
        if link.business_category_id:
            tags['category_id'] = link.business_category_id
            tags['category_name'] = link.business_category.name
        rows.append(UndocumentedRow(
            source=SOURCE_PAYMENT_LINKS,
            customer=payment.payer_name or 'ללא שם',
            reference=str(payment.id)[:8].upper(),
            row_date=(payment.paid_at or payment.created_at).date(),
            detail=f'{link.title} · {payment.option_label}'.strip(' ·'),
            branch_id=branch_id,
            branch_name=branch_name,
            amount=payment.amount,
            child_ids=[],
            **tags,
        ))
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
    delivery = _delivery_tags()
    for source, rows in (
        (SOURCE_LESSONS, _lesson_rows(branch_ids, start, end)),
        (SOURCE_STORE, _store_rows(branch_ids, start, end, delivery)),
        (SOURCE_ORPHAN_CHARGES, _orphan_charge_rows(branch_ids, start, end)),
        (SOURCE_PAYMENT_LINKS, _payment_link_rows(branch_ids, start, end)),
    ):
        if rows:
            sections.append(SourceSection(source=source, label=SOURCE_LABELS[source], rows=rows))

    income = UndocumentedIncome(sections=sections)
    merge_against_documents(income, start, end)
    return income
