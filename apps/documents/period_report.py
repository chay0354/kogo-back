"""Period invoice report — selects existing FormalDocument rows and groups them.

Read-only by construction: nothing here writes, and no stored amount is ever
recomputed. Every figure on the report is a stored column summed as-is, so a
row's numbers always reconcile with the document record they came from.

That last point is not pedantry. `service._compute_totals` applies `round_total`
to `total_amount` only, so `subtotal - discount_amount + vat_amount` does not
always equal `total_amount` on a rounded document. Deriving any column here
would therefore disagree with the issued document. Each column is summed
independently instead.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q

from apps.core.scoping import (
    ACTIVE_ENROLLMENT_STATUSES,
    is_scoped_partner,
    partner_branch_ids,
)
from apps.documents.models import DOCUMENT_TYPE_CHOICES, FormalDocument

GROUP_BY_BRANCH = 'branch'
GROUP_BY_BUSINESS = 'business'
GROUP_BY_UNIT = 'business_unit'
GROUP_BY_CATEGORY = 'business_category'
GROUP_BY_CHOICES = (GROUP_BY_BRANCH, GROUP_BY_BUSINESS, GROUP_BY_UNIT, GROUP_BY_CATEGORY)
UNTAGGED_BUSINESS_LABEL = 'ללא שיוך לעסק'
UNTAGGED_CATEGORY_LABEL = 'ללא קטגוריה'

# A document that reaches neither a branch nor a business customer still has to
# appear somewhere, so each grouping has a named bucket for it. Dropping such a
# row would make the report's total disagree with the ledger silently.
UNASSIGNED_BRANCH_LABEL = 'ללא שיוך לסניף'
PRIVATE_CUSTOMERS_LABEL = 'לקוחות פרטיים'

CREDIT_TYPE = 'credit_invoice'

DOCUMENT_TYPE_LABELS = dict(DOCUMENT_TYPE_CHOICES)

# The order an accountant reads them in: what was charged, what was collected,
# what is only a demand for payment, and what was taken back.
TYPE_ORDER = ('tax_invoice', 'combined', 'receipt', 'transaction_invoice', CREDIT_TYPE)

# A tax invoice and the receipt that settles it are two documents for one sum,
# so the period is described by three figures rather than one grand total:
# revenue recognised (tax documents, credits deducted), cash collected
# (receipts, and the invoice-receipt which is both), and demands for payment
# that are not tax documents at all.
REVENUE_TYPES = ('tax_invoice', 'combined')
COLLECTION_TYPES = ('receipt', 'combined')
NON_FISCAL_TYPES = ('transaction_invoice',)

HEBREW_MONTHS = [
    'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני',
    'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר',
]

class ReportInputError(ValueError):
    """A bad period or grouping in the request — a 400, not a server error."""


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of a calendar month."""
    if month < 1 or month > 12:
        raise ReportInputError('חודש לא תקין')
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def parse_period(params) -> tuple[date, date, str]:
    """
    Resolve the reporting period from query params.

    `month=YYYY-MM` is the owner-facing case. `start_date`/`end_date` are
    accepted too so the same report can cover a quarter or a custom range.
    Returns (start, end, hebrew_label).
    """
    raw_month = (params.get('month') or '').strip()
    if raw_month:
        try:
            year_s, month_s = raw_month.split('-')
            year, month = int(year_s), int(month_s)
        except (ValueError, TypeError):
            raise ReportInputError('פורמט חודש לא תקין — נדרש YYYY-MM')
        start, end = month_bounds(year, month)
        return start, end, f'{HEBREW_MONTHS[month - 1]} {year}'

    raw_start = (params.get('start_date') or '').strip()
    raw_end = (params.get('end_date') or '').strip()
    if not raw_start or not raw_end:
        raise ReportInputError('נדרש חודש (month=YYYY-MM) או טווח תאריכים')
    try:
        start = date.fromisoformat(raw_start)
        end = date.fromisoformat(raw_end)
    except ValueError:
        raise ReportInputError('פורמט תאריך לא תקין — נדרש YYYY-MM-DD')
    if end < start:
        raise ReportInputError('תאריך הסיום מוקדם מתאריך ההתחלה')
    return start, end, f'{start.strftime("%d/%m/%Y")} — {end.strftime("%d/%m/%Y")}'


def _partner_branch_q(branch_ids) -> Q:
    """
    Documents whose *resolved* branch is one of `branch_ids`.

    Each arm repeats the negations of the arms above it so the priority order
    matches resolve_branch exactly. Without that, a document owned by branch A
    through its direct FK would also match branch B through its child's
    enrollment, and a partner at B would be handed a document that the report
    itself files under A.
    """
    direct = Q(branch_id__in=branch_ids)
    no_direct = Q(branch__isnull=True)
    business = no_direct & Q(business_customer__branch_id__in=branch_ids)
    no_business = no_direct & Q(business_customer__branch__isnull=True)
    family = no_business & Q(child__family__branch_id__in=branch_ids)
    no_family = no_business & Q(child__family__branch__isnull=True)
    enrolled = no_family & Q(
        child__lesson_enrollments__status__in=ACTIVE_ENROLLMENT_STATUSES,
        child__lesson_enrollments__lesson__course__branch_id__in=branch_ids,
    )
    return direct | business | family | enrolled


def scoped_documents(user, start: date, end: date, document_type: str = '') -> tuple:
    """
    The documents `user` may see in the period, already narrowed at the database.

    Returns (queryset, partner_branch_ids_or_None). A partner with no assigned
    branch gets nothing, matching the fail-closed rule the rest of the scoping
    layer follows.
    """
    qs = (
        FormalDocument.objects
        .filter(document_date__gte=start, document_date__lte=end)
        .select_related(
            'branch', 'business', 'business_category',
            'business_customer', 'business_customer__branch',
            'child', 'child__family', 'child__family__branch',
        )
        .prefetch_related('payments', 'store_invoices')
    )
    qs = qs.exclude(document_type='draft')
    if document_type:
        qs = qs.filter(document_type=document_type)

    if not is_scoped_partner(user):
        return qs, None

    branch_ids = partner_branch_ids(user)
    if not branch_ids:
        return qs.none(), []
    # distinct(): the enrollment arm joins a to-many relation and would
    # otherwise repeat a document once per matching enrollment.
    return qs.filter(_partner_branch_q(branch_ids)).distinct(), branch_ids


def _enrollment_branch_map(documents) -> dict:
    """
    child_id -> (branch_id, branch_name) via an active enrollment.

    Built in one query for every child that still needs a branch, rather than
    walking each child's enrollments, so the report stays flat in query count
    regardless of how many documents the period holds.
    """
    from apps.enrollments.models import LessonEnrollment

    pending = {
        doc.child_id for doc in documents
        if doc.child_id
        and doc.branch_id is None
        and not (doc.business_customer and doc.business_customer.branch_id)
        and not (doc.child and doc.child.family and doc.child.family.branch_id)
    }
    if not pending:
        return {}

    rows = (
        LessonEnrollment.objects
        .filter(
            child_id__in=pending,
            status__in=ACTIVE_ENROLLMENT_STATUSES,
            lesson__course__branch__isnull=False,
        )
        .select_related('lesson__course__branch')
        .order_by('child_id', '-updated_at')
    )
    mapping = {}
    for row in rows:
        # order_by puts the most recent enrollment first; keep that one.
        if row.child_id in mapping:
            continue
        branch = row.lesson.course.branch
        mapping[row.child_id] = (branch.id, branch.name)
    return mapping


def resolve_branch(doc, enrollment_map: dict) -> tuple:
    """
    The branch a document belongs to, and how that was established.

    The direct FK wins when set, but it rarely is: CreateDocumentSerializer
    declares branch_id as an IntegerField while Branch.id is a UUID, so the
    field cannot survive validation and documents are stored with branch NULL.
    The fallbacks below are what actually files a document in practice.
    """
    if doc.branch_id:
        return doc.branch_id, doc.branch.name, 'direct'
    if doc.business_customer and doc.business_customer.branch_id:
        return doc.business_customer.branch_id, doc.business_customer.branch.name, 'business_customer'
    if doc.child and doc.child.family and doc.child.family.branch_id:
        return doc.child.family.branch_id, doc.child.family.branch.name, 'family'
    if doc.child_id and doc.child_id in enrollment_map:
        branch_id, branch_name = enrollment_map[doc.child_id]
        return branch_id, branch_name, 'enrollment'
    return None, UNASSIGNED_BRANCH_LABEL, 'unassigned'


def customer_name(doc) -> str:
    """The name that goes on the row — the owner asked for this explicitly."""
    if doc.business_customer_id and doc.business_customer:
        return doc.business_customer.full_name or 'לקוח עסקי'
    if doc.child_id and doc.child:
        return doc.child.full_name or 'לקוח'
    # A credit note issued with a refund names its customer on the document itself.
    return (getattr(doc, 'customer_name', None) or '').strip() or 'ללא שם לקוח'


@dataclass
class ReportRow:
    customer: str
    document_number: str
    document_type: str
    document_type_label: str
    document_date: date
    subtotal: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    total_amount: Decimal
    is_credit: bool
    currency: str
    vat_exempt: bool
    # Where the row came from and what it points at. The PDF prints none of
    # these; the register export and the uniform-format files read them.
    channel: str = 'manual'
    source_id: str = ''
    reference: str = ''
    payment_method: str = ''
    branch_name: str = ''
    business_name: str = ''
    category_name: str = ''
    # Another document that may cover the same payment (register.find_possible_duplicates).
    duplicate_of: str = ''
    # A number handed to a sale that never became a document: listed, never summed.
    void: bool = False
    void_reason: str = ''


@dataclass
class Totals:
    """Column sums. Every field is a sum of stored values, never a derivation."""
    count: int = 0
    subtotal: Decimal = Decimal('0.00')
    discount_amount: Decimal = Decimal('0.00')
    net_amount: Decimal = Decimal('0.00')
    vat_amount: Decimal = Decimal('0.00')
    total_amount: Decimal = Decimal('0.00')
    charges_total: Decimal = Decimal('0.00')
    credits_total: Decimal = Decimal('0.00')

    def add(self, row: ReportRow) -> None:
        # A credit is printed with its sign on the row above the summary, so
        # the columns must sum to what a reader adds up by eye. Summing the
        # raw positives showed a branch's take as charges plus its refunds.
        # charges_total and credits_total stay absolute for the breakdown.
        sign = -1 if row.is_credit else 1
        self.count += 1
        self.subtotal += sign * row.subtotal
        self.discount_amount += sign * row.discount_amount
        self.net_amount += sign * row.net_amount
        self.vat_amount += sign * row.vat_amount
        self.total_amount += sign * row.total_amount
        if row.is_credit:
            self.credits_total += row.total_amount
        else:
            self.charges_total += row.total_amount

    @property
    def net_of_credits(self) -> Decimal:
        """Charges minus credits — the figure the owner reads as the period's take."""
        return self.charges_total - self.credits_total


@dataclass
class TypeSection:
    """One document type's rows inside a group, with its own subtotal."""
    document_type: str
    label: str
    rows: list = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)


@dataclass
class ReportGroup:
    key: object
    title: str
    rows: list = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    # True for the catch-all bucket, so the PDF can say why those rows are there.
    is_unassigned: bool = False

    @property
    def collected_total(self) -> Decimal:
        """
        Money that arrived in this group against a document.

        Receipts and invoice-receipts only. A tax invoice is left out on
        purpose: it recognises revenue, and the receipt that settles it is the
        same money a second time. Credits are not deducted here — the report
        keeps them a column of their own, the way the totals panel does.
        """
        return sum(
            (row.total_amount for row in self.rows
             if not row.is_credit and row.document_type in COLLECTION_TYPES),
            Decimal('0.00'),
        )

    @property
    def sections(self) -> list:
        """Rows split by document type, in reading order, each with a subtotal."""
        by_type: dict = {}
        for row in self.rows:
            section = by_type.get(row.document_type)
            if section is None:
                section = TypeSection(row.document_type, row.document_type_label)
                by_type[row.document_type] = section
            section.rows.append(row)
            section.totals.add(row)
        rank = {code: index for index, code in enumerate(TYPE_ORDER)}
        return sorted(by_type.values(), key=lambda sec: rank.get(sec.document_type, len(rank)))


@dataclass
class PeriodReport:
    start: date
    end: date
    period_label: str
    group_by: str
    groups: list
    totals: Totals
    type_totals: dict
    document_type: str = ''
    scope_label: str = ''
    currencies: set = field(default_factory=set)
    # מה שטרנזילה מדווחת לאותה תקופה מול מה שיש אצלנו. None = לא נבדק.
    reconciliation: dict = None
    # תקבולים בתקופה שלא הופק להם מסמך פורמלי. None = לא נאסף.
    undocumented: object = None
    # Numbers handed to sales that never became a document (register.py).
    void_rows: list = field(default_factory=list)
    # Every run of the years the period touches, checked for gaps (numbering.continuity).
    continuity: list = field(default_factory=list)
    # A lesson receipt and a document issued by hand that may cover one payment.
    possible_duplicates: list = field(default_factory=list)

    def _sum_types(self, codes, credits: bool = False) -> Decimal:
        total = sum((self.type_totals[c].total_amount for c in codes if c in self.type_totals), Decimal('0.00'))
        if credits and CREDIT_TYPE in self.type_totals:
            total -= self.type_totals[CREDIT_TYPE].credits_total
        return total

    @property
    def revenue_total(self) -> Decimal:
        """Tax invoices and invoice-receipts, less credit invoices."""
        return self._sum_types(REVENUE_TYPES, credits=True)

    @property
    def collected_total(self) -> Decimal:
        """Receipts and invoice-receipts — money that actually arrived."""
        return self._sum_types(COLLECTION_TYPES)

    @property
    def non_fiscal_total(self) -> Decimal:
        """Transaction invoices: a demand for payment, not a tax document."""
        return self._sum_types(NON_FISCAL_TYPES)

    @property
    def is_empty(self) -> bool:
        return self.totals.count == 0

    def income_by_group(self) -> list:
        """
        Per group: what came in with a document, what came in without one, and
        the sum of the two. The report's last word on 'how much did this branch
        actually take', which is the question the owner opens it with.
        """
        rows: dict = {}
        for group in self.groups:
            # The catch-all bucket has no id; its title is the key both sides use.
            key = group.key if group.key is not None else group.title
            rows[key] = {
                'title': group.title,
                'documented': group.collected_total,
                'credits': group.totals.credits_total,
                'undocumented': Decimal('0.00'),
            }
        if self.undocumented is not None:
            for key, (name, amount) in self.undocumented.by_group(self.group_by).items():
                entry = rows.get(key)
                if entry is None:
                    # A branch that took money in but issued no document at all
                    # has no group above; it still belongs in this table.
                    entry = {'title': name, 'documented': Decimal('0.00'),
                             'credits': Decimal('0.00'), 'undocumented': Decimal('0.00')}
                    rows[key] = entry
                entry['undocumented'] += amount
        for entry in rows.values():
            entry['total'] = entry['documented'] + entry['undocumented'] - entry['credits']
        return sorted(
            rows.values(),
            key=lambda entry: (1 if entry['title'] == UNASSIGNED_BRANCH_LABEL else 0, entry['title']),
        )

    @property
    def undocumented_total(self) -> Decimal:
        """Money taken in the period with no document behind it — 0 when not collected."""
        if self.undocumented is None:
            return Decimal('0.00')
        return self.undocumented.total

    @property
    def all_income_total(self) -> Decimal:
        """
        Everything that came in: documented collection plus undocumented charges.

        Kept apart from `collected_total` on purpose: that figure is what the
        documents say, and this one adds the charges no document was raised
        for. Credits come off, since a period's take is not what was charged
        but what stayed.
        """
        return self.collected_total + self.undocumented_total - self.totals.credits_total


def _row_from(doc) -> ReportRow:
    net = doc.subtotal - doc.discount_amount
    # A store sale's Tranzila copy points at the sale; a credit note at what it credits.
    store_sale = next(iter(doc.store_invoices.all()), None)
    payment = next(iter(doc.payments.all()), None)
    return ReportRow(
        channel='manual',
        source_id=str(doc.id),
        reference=store_sale.invoice_number if store_sale else (doc.linked_document_number or ''),
        payment_method=payment.payment_method if payment else '',
        business_name=doc.business.name if doc.business_id else '',
        category_name=doc.business_category.name if doc.business_category_id else '',
        customer=customer_name(doc),
        document_number=doc.document_number,
        document_type=doc.document_type,
        document_type_label=DOCUMENT_TYPE_LABELS.get(doc.document_type, doc.document_type),
        document_date=doc.document_date,
        subtotal=doc.subtotal,
        discount_amount=doc.discount_amount,
        net_amount=net,
        vat_amount=doc.vat_amount,
        total_amount=doc.total_amount,
        is_credit=doc.document_type == CREDIT_TYPE,
        currency=doc.currency,
        vat_exempt=doc.vat_exempt,
    )


def build_report(
    user,
    start: date,
    end: date,
    period_label: str,
    group_by: str = GROUP_BY_BRANCH,
    document_type: str = '',
) -> PeriodReport:
    """
    Group every document in the period into exactly one bucket, and total it.

    The documents come from every channel (register.py): the documents module,
    and the lesson receipts and store sales numbered in consecutive runs. A number
    that never became a document is kept apart — listed, never summed.
    """
    # Imported here: each of these modules imports this one.
    from apps.documents.numbering import continuity
    from apps.documents.register import channel_documents, find_possible_duplicates
    from apps.documents.undocumented_income import RANK_UNASSIGNED, _group_key

    if group_by not in GROUP_BY_CHOICES:
        raise ReportInputError('קיבוץ לא נתמך')

    qs, branch_ids = scoped_documents(user, start, end, document_type)
    documents = list(qs.order_by('document_date', 'document_number'))
    enrollment_map = _enrollment_branch_map(documents)

    placed = []  # (row, group key, group title, is the catch-all bucket)
    manual_rows = {}
    for doc in documents:
        row = _row_from(doc)
        branch_key, branch_title, _source = resolve_branch(doc, enrollment_map)
        row.branch_name = branch_title if branch_key is not None else ''

        if group_by == GROUP_BY_BRANCH:
            key, title = branch_key, branch_title
            unassigned = key is None
        elif group_by == GROUP_BY_UNIT:
            key = doc.business_id
            title = doc.business.name if doc.business_id else UNTAGGED_BUSINESS_LABEL
            unassigned = key is None
        elif group_by == GROUP_BY_CATEGORY:
            key = doc.business_category_id
            if doc.business_category_id:
                title = f'{doc.business.name} · {doc.business_category.name}'
            else:
                title = UNTAGGED_CATEGORY_LABEL
            unassigned = key is None
        else:
            if doc.business_customer_id:
                key = doc.business_customer_id
                title = doc.business_customer.full_name or 'לקוח עסקי'
                unassigned = False
            else:
                # Documents issued to a registered child are not business
                # customers at all; they belong together rather than nowhere.
                key = None
                title = PRIVATE_CUSTOMERS_LABEL
                unassigned = True
        placed.append((row, key, title, unassigned))
        manual_rows[doc.document_number] = row

    # A partner with no branch was handed no documents above, and no receipts here.
    channel = channel_documents(branch_ids, start, end, document_type) if branch_ids != [] else []
    void_rows = []
    for item in channel:
        if item.row.void:
            void_rows.append(item.row)
            continue
        key, title, rank = _group_key(item, group_by)
        unassigned = rank == RANK_UNASSIGNED
        # The document side keys its catch-all bucket None: one bucket, not two.
        placed.append((item.row, None if unassigned else key, title, unassigned))

    duplicates = find_possible_duplicates(channel, documents)
    channel_rows = {item.row.document_number: item.row for item in channel}
    for pair in duplicates:
        channel_rows[pair['number']].duplicate_of = pair['other']
        if pair['other'] in manual_rows:
            manual_rows[pair['other']].duplicate_of = pair['number']

    groups: dict = {}
    overall = Totals()
    type_totals: dict = {}
    currencies = set()
    for row, key, title, unassigned in sorted(
        placed, key=lambda entry: (entry[0].document_date, entry[0].document_number),
    ):
        currencies.add(row.currency)
        group = groups.get(key)
        if group is None:
            group = ReportGroup(key=key, title=title, is_unassigned=unassigned)
            groups[key] = group
        group.rows.append(row)
        group.totals.add(row)
        overall.add(row)

        bucket = type_totals.setdefault(row.document_type, Totals())
        bucket.add(row)

    # Named groups first, alphabetically; the catch-all bucket last so a reader
    # scanning for a branch is not interrupted by it.
    ordered = sorted(
        groups.values(),
        key=lambda g: (1 if g.is_unassigned else 0, g.title),
    )

    if branch_ids is None:
        scope_label = 'כל הסניפים'
    else:
        names = sorted({g.title for g in ordered if not g.is_unassigned})
        scope_label = 'סניפים משויכים: ' + (', '.join(names) if names else 'ללא')

    # The runs are the whole business's, so only a reader who sees all of it gets them.
    runs = []
    if branch_ids is None:
        for year in range(start.year, end.year + 1):
            runs.extend(continuity(year))

    return PeriodReport(
        start=start,
        end=end,
        period_label=period_label,
        group_by=group_by,
        groups=ordered,
        totals=overall,
        type_totals=type_totals,
        document_type=document_type,
        scope_label=scope_label,
        currencies=currencies,
        void_rows=sorted(void_rows, key=lambda row: (row.document_date, row.document_number)),
        continuity=runs,
        possible_duplicates=duplicates,
    )
