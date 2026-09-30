"""
Receipts against invoices: what each invoice still owes (finding C, 30.9.2026).

An invoice is closed by the documents that pay it and by the credit notes that
take it back. Until now nothing recorded which receipt paid which invoice, so
the collections tab showed every tax invoice open for good.

    open = total − what paid it − what credited it   (never below zero)

What paid it is read from four places, so the documents already issued in
production need no backfill:

1. DocumentSettlement rows (S0, documents/0013) — written from now on when a
   receipt (RC) pays a tax invoice (TI), when an invoice-receipt (IRM) pays a
   transaction invoice (TX), and when a check plan's or an old cash plan's
   invoice is issued against the plan's receipt. A voided row pays nothing
   and is never deleted.
2. A receipt that named the invoice by number before settlements existed
   (receipt_details.linked_invoice_id → linked_document_number), of the same
   customer, and that has no settlement row of its own. Capped at the invoice.
3. A check plan's invoice issued before settlements existed: the check that
   paid it is on the plan's receipt (unless the check bounced).
4. An old cash plan's monthly tax invoice issued before settlements existed:
   the cash is on the plan's receipt.

Once an invoice has any settlement row (active or voided), rules 3 and 4 no
longer apply to it — what the office recorded or voided since is what counts.

What credited it: credit notes that name it (linked_document, or its number
in linked_document_number) — the link WS-2 already checks for type, customer
and cap. A credit note writes no settlement row: its link is the one record.

Only kogo's own tax invoices and transaction invoices carry a balance here. A
lesson receipt, a store sale, an RT rent receipt and an IRM are paid in the
act of issuing them; a store's monthly-billing sale (SD) keeps its own
amount_paid; the previous software's documents are history (legacy_import).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.documents.models import (
    CashPlanMonth,
    CheckItem,
    DocumentSettlement,
    FormalDocument,
)

ZERO = Decimal('0')
CENT = Decimal('0.01')

# What pays what. A receipt closes a tax invoice: the invoice carried the VAT,
# the receipt the money. A transaction invoice is a demand for payment, and
# the tax invoice has to be issued with the payment — so it is closed by a
# חשבונית מס/קבלה, never by a bare receipt (that would leave the VAT
# uninvoiced), and a tax invoice is never closed by an invoice-receipt (that
# would charge its VAT twice).
PAYER_FOR = {
    'receipt': ('tax_invoice',),
    'combined': ('transaction_invoice',),
}
INVOICE_TYPES = ('tax_invoice', 'transaction_invoice')
PAYER_TYPES = tuple(PAYER_FOR)

SOURCE_SETTLEMENT = 'settlement'
SOURCE_LINKED_RECEIPT = 'linked_receipt'
SOURCE_CHECK_PLAN = 'check_plan'
SOURCE_CASH_PLAN = 'cash_plan'

STATUS_OPEN = 'open'
STATUS_PARTIAL = 'partial'
STATUS_PAID = 'paid'
STATUS_CREDITED = 'credited'
STATUS_LABELS = {
    STATUS_OPEN: 'פתוחה',
    STATUS_PARTIAL: 'שולמה חלקית',
    STATUS_PAID: 'שולמה',
    STATUS_CREDITED: 'זוכתה',
}


class SettlementError(ValueError):
    """A settlement the rules refuse. The message is the office's, in Hebrew; the view answers 400."""


class SettlementVoided(Exception):
    """The settlement was voided already — it is not voided twice (409)."""

    def __init__(self, at):
        super().__init__(at)
        self.at = at


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


def money_text(amount) -> str:
    return f'{money(amount):,.2f} ₪'


@dataclass(frozen=True)
class Fact:
    """One payer paying one invoice `amount` — a settlement row, or what an older record implies."""
    payer_id: object
    invoice_id: object
    amount: Decimal
    source: str
    settlement_id: object = None


@dataclass
class Balance:
    invoice_id: object
    total: Decimal
    paid: Decimal = ZERO
    credited: Decimal = ZERO
    facts: list = field(default_factory=list)

    @property
    def open(self) -> Decimal:
        return money(max(ZERO, self.total - self.paid - self.credited))

    @property
    def status(self) -> str:
        if self.open > 0:
            return STATUS_PARTIAL if (self.paid > 0 or self.credited > 0) else STATUS_OPEN
        if self.paid <= 0 and self.credited > 0:
            return STATUS_CREDITED
        return STATUS_PAID

    def as_dict(self) -> dict:
        return {
            'total': str(money(self.total)),
            'paid': str(money(self.paid)),
            'credited': str(money(self.credited)),
            'open': str(self.open),
            'status': self.status,
            'status_label': STATUS_LABELS[self.status],
        }


def _same_customer(a: FormalDocument, b: FormalDocument) -> bool:
    return (a.child_id, a.business_customer_id) == (b.child_id, b.business_customer_id)


def _payers_with_rows(payer_ids) -> set:
    """Payers with any settlement row: their linked number (rule 2) is no longer read."""
    return set(DocumentSettlement.objects.filter(payer_id__in=payer_ids).values_list('payer_id', flat=True))


def _plan_facts(*, invoice_ids=None, payer_ids=None) -> list[Fact]:
    """Rules 3 and 4: check-plan and old cash-plan invoices issued before settlements existed."""
    facts: list[Fact] = []
    checks = CheckItem.objects.filter(
        tax_invoice__isnull=False, bounced_at__isnull=True, plan__receipt__isnull=False,
    ).exclude(tax_invoice_id__in=DocumentSettlement.objects.values('invoice_id'))
    months = CashPlanMonth.objects.filter(
        document__isnull=False, document__document_type='tax_invoice', plan__receipt__isnull=False,
    ).exclude(document_id__in=DocumentSettlement.objects.values('invoice_id'))
    if invoice_ids is not None:
        checks = checks.filter(tax_invoice_id__in=invoice_ids)
        months = months.filter(document_id__in=invoice_ids)
    if payer_ids is not None:
        checks = checks.filter(plan__receipt_id__in=payer_ids)
        months = months.filter(plan__receipt_id__in=payer_ids)
    for receipt_id, invoice_id, amount, invoice_total in checks.values_list(
        'plan__receipt_id', 'tax_invoice_id', 'amount', 'tax_invoice__total_amount',
    ):
        facts.append(Fact(receipt_id, invoice_id, min(money(amount), money(invoice_total)), SOURCE_CHECK_PLAN))
    for receipt_id, invoice_id, amount, invoice_total in months.values_list(
        'plan__receipt_id', 'document_id', 'amount', 'document__total_amount',
    ):
        facts.append(Fact(receipt_id, invoice_id, min(money(amount), money(invoice_total)), SOURCE_CASH_PLAN))
    return facts


def _linked_facts(payers, invoices) -> list[Fact]:
    """Rule 2: a receipt that named an invoice's number, of the same customer, with no settlement row."""
    facts = []
    by_number = {doc.document_number: doc for doc in invoices}
    for payer in payers:
        invoice = by_number.get((payer.linked_document_number or '').strip())
        if invoice is None or invoice.pk == payer.pk or not _same_customer(payer, invoice):
            continue
        facts.append(Fact(
            payer.pk, invoice.pk, min(money(payer.total_amount), money(invoice.total_amount)), SOURCE_LINKED_RECEIPT,
        ))
    return facts


def _explicit_facts(rows) -> list[Fact]:
    return [
        Fact(row['payer_id'], row['invoice_id'], money(row['amount']), SOURCE_SETTLEMENT, row['id'])
        for row in rows if row['voided_at'] is None
    ]


def facts_for_invoices(invoices) -> list[Fact]:
    """Everything that paid `invoices` (FormalDocuments), by the four rules above."""
    invoices = [doc for doc in invoices if doc.document_type in INVOICE_TYPES]
    if not invoices:
        return []
    ids = [doc.pk for doc in invoices]
    facts = _explicit_facts(
        DocumentSettlement.objects.filter(invoice_id__in=ids).values('id', 'payer_id', 'invoice_id', 'amount', 'voided_at')
    )
    linked = list(
        FormalDocument.objects
        .filter(document_type__in=PAYER_TYPES, linked_document_number__in=[doc.document_number for doc in invoices])
        .exclude(pk__in=DocumentSettlement.objects.values('payer_id'))
        .only('id', 'document_type', 'linked_document_number', 'total_amount', 'child_id', 'business_customer_id')
    )
    facts += _linked_facts(linked, invoices)
    facts += _plan_facts(invoice_ids=ids)
    return facts


def facts_for_payers(payers) -> list[Fact]:
    """Everything `payers` (receipts, invoice-receipts) paid, by the same four rules."""
    payers = [doc for doc in payers if doc.document_type in PAYER_TYPES]
    if not payers:
        return []
    ids = [doc.pk for doc in payers]
    facts = _explicit_facts(
        DocumentSettlement.objects.filter(payer_id__in=ids).values('id', 'payer_id', 'invoice_id', 'amount', 'voided_at')
    )
    with_rows = _payers_with_rows(ids)
    unlinked = [doc for doc in payers if doc.pk not in with_rows and (doc.linked_document_number or '').strip()]
    if unlinked:
        invoices = list(
            FormalDocument.objects
            .filter(document_type__in=INVOICE_TYPES,
                    document_number__in=[doc.linked_document_number.strip() for doc in unlinked])
            .only('id', 'document_number', 'document_type', 'total_amount', 'child_id', 'business_customer_id')
        )
        facts += _linked_facts(unlinked, invoices)
    facts += _plan_facts(payer_ids=ids)
    return facts


def _credits(invoices) -> dict:
    """Credit notes' totals per invoice: by the FK when it is set, else by the number they name."""
    by_number = {doc.document_number: doc.pk for doc in invoices}
    ids = set(by_number.values())
    out: dict = defaultdict(lambda: ZERO)
    for linked_id, linked_number, total in (
        FormalDocument.objects
        .filter(document_type='credit_invoice')
        .filter(Q(linked_document_id__in=ids) | Q(linked_document__isnull=True, linked_document_number__in=list(by_number)))
        .values_list('linked_document_id', 'linked_document_number', 'total_amount')
    ):
        invoice_id = linked_id if linked_id in ids else by_number.get((linked_number or '').strip())
        if invoice_id is not None:
            out[invoice_id] += money(total)
    return out


def balances(invoices) -> dict:
    """{invoice pk: Balance} for kogo's tax invoices and transaction invoices among `invoices`."""
    invoices = [doc for doc in invoices if doc.document_type in INVOICE_TYPES]
    result = {doc.pk: Balance(invoice_id=doc.pk, total=money(doc.total_amount)) for doc in invoices}
    if not result:
        return result
    for fact in facts_for_invoices(invoices):
        line = result.get(fact.invoice_id)
        if line is not None:
            line.paid += fact.amount
            line.facts.append(fact)
    for invoice_id, credited in _credits(invoices).items():
        result[invoice_id].credited += credited
    return result


def balance_of(invoice: FormalDocument):
    """The Balance of one invoice, or None for a document that carries none."""
    return balances([invoice]).get(invoice.pk)


def applied_amounts(payers) -> dict:
    """{payer pk: how much of it went to invoices} — the part a report must not count twice."""
    out: dict = defaultdict(lambda: ZERO)
    for fact in facts_for_payers(payers):
        out[fact.payer_id] += fact.amount
    return out


def payer_capacity(payer: FormalDocument) -> Decimal:
    """
    How much a payer can settle. A receipt: what it received plus the ניכוי
    במקור the customer withheld (the certificate pays that part of the invoice;
    WS-2 keeps a receipt's total at the money that arrived). An invoice-receipt:
    its total — its payments and withholding already add up to it.
    """
    if payer.document_type == 'receipt':
        return money(payer.total_amount) + money(payer.withholding_amount)
    return money(payer.total_amount)


# ── Writing ──────────────────────────────────────────────────────────────────

def _type_refusal(payer: FormalDocument, invoice: FormalDocument) -> str:
    number = invoice.document_number
    if payer.document_type == 'receipt' and invoice.document_type == 'transaction_invoice':
        return (f'{number} הוא חשבונית עסקה. חשבונית עסקה נסגרת בחשבונית מס/קבלה — קבלה לבדה '
                'אינה מפיקה את חשבונית המס שחייבת לצאת עם התשלום.')
    if payer.document_type == 'combined' and invoice.document_type == 'tax_invoice':
        return (f'{number} היא חשבונית מס. חשבונית מס נסגרת בקבלה — חשבונית מס/קבלה עליה '
                'הייתה מחייבת את המע"מ פעם שנייה.')
    return (f'{number} הוא {invoice.get_document_type_display()}, ואין בו יתרה לסגור. נסגרות רק '
            'חשבונית מס (בקבלה) וחשבונית עסקה (בחשבונית מס/קבלה).')


def _actor(user):
    return user if getattr(user, 'is_authenticated', False) else None


def _wanted(rows) -> list[tuple[str, Decimal]]:
    """[(invoice id, amount)] from [{invoice_id, amount}] — every amount above zero."""
    wanted = []
    for row in rows or []:
        amount = money(row.get('amount'))
        if amount <= 0:
            raise SettlementError('הסכום שנסגר בכל חשבונית חייב להיות גדול מאפס')
        wanted.append((str(row.get('invoice_id') or ''), amount))
    return wanted


@transaction.atomic
def record_settlements(payer: FormalDocument, rows, *, user=None) -> list[DocumentSettlement]:
    """
    Record that `payer` pays each invoice in `rows` ([{invoice_id, amount}]).

    Checked under row locks — the invoices first, in a fixed order, then the
    payer — so two receipts for one invoice are checked one after the other:
    each invoice is the payer's own customer's and of the type the payer
    closes (PAYER_FOR); each amount is above zero and within what the invoice
    still owes; together they are within what the payer can settle
    (payer_capacity), less what it already settled. Raises SettlementError
    with the reason; the caller's transaction then rolls the payer back too,
    number and all.
    """
    wanted = _wanted(rows)
    if not wanted:
        return []
    payer, invoices = _checked(payer, wanted)
    return [
        DocumentSettlement.objects.create(
            payer=payer,
            invoice=invoices[invoice_id],
            invoice_number=invoices[invoice_id].document_number,
            invoice_kind=DocumentSettlement.KIND_FORMAL,
            amount=amount,
            created_by=_actor(user),
        )
        for invoice_id, amount in wanted
    ]


def check_draft_settlements(draft: FormalDocument, target_type: str, rows) -> list[dict]:
    """
    The settlements a draft receipt or invoice-receipt will record when it is
    approved ([{invoice_id, invoice_number, amount}]), checked now by the rules
    of record_settlements as if it were already a `target_type` — so the
    office hears at once that an invoice is closed, or another customer's.

    Nothing is written: a draft pays nothing, and an invoice it names stays
    open (and open to other receipts) until it is approved. The approval
    checks them again, against the balances of that moment
    (service.finalize_draft → settle_on_issue).
    """
    wanted = _wanted(rows)
    if not wanted:
        return []
    # The draft as the document it will become: its type, customer and what it
    # can settle. Unsaved — never a row, never a payer in any balance.
    probe = FormalDocument(
        pk=draft.pk,
        document_number=draft.document_number,
        document_type=target_type,
        child_id=draft.child_id,
        business_customer_id=draft.business_customer_id,
        total_amount=draft.total_amount,
        withholding_amount=draft.withholding_amount,
    )
    _, invoices = _checked(probe, wanted, lock_payer=False)
    return [
        {'invoice_id': invoice_id, 'invoice_number': invoices[invoice_id].document_number, 'amount': str(amount)}
        for invoice_id, amount in wanted
    ]


def _checked(payer: FormalDocument, wanted, *, lock_payer: bool = True):
    """
    record_settlements' checks, under the invoices' row locks (then the
    payer's). Returns (payer as read under its lock, {invoice id: invoice});
    raises SettlementError with the reason.
    """
    allowed = PAYER_FOR.get(payer.document_type)
    if allowed is None:
        raise SettlementError('חשבונית נסגרת בקבלה או בחשבונית מס/קבלה בלבד')
    ids = [invoice_id for invoice_id, _ in wanted]
    if len(set(ids)) != len(ids):
        raise SettlementError('אותה חשבונית נבחרה פעמיים. כל חשבונית נרשמת פעם אחת, בסכום ששולם עליה.')

    invoices = {
        str(doc.pk): doc
        for doc in FormalDocument.objects.select_for_update().filter(pk__in=ids).order_by('pk')
    }
    if lock_payer:
        payer = FormalDocument.objects.select_for_update().get(pk=payer.pk)
    for invoice_id, _ in wanted:
        invoice = invoices.get(invoice_id)
        if invoice is None:
            raise SettlementError('החשבונית שנבחרה לא נמצאה')
        if invoice.document_type not in allowed:
            raise SettlementError(_type_refusal(payer, invoice))
        if not _same_customer(payer, invoice):
            raise SettlementError(
                f'{invoice.document_number} הונפקה ללקוח אחר. מסמך סוגר רק חשבוניות של הלקוח שעליו הוא מופק.'
            )

    # The payer's own older link (rule 2) is what these rows replace: it is
    # not counted against them, and it stops counting once they are written.
    current = balances(invoices.values())
    for invoice_id, amount in wanted:
        invoice = invoices[invoice_id]
        line = current[invoice.pk]
        own = sum((f.amount for f in line.facts if f.payer_id == payer.pk and f.source == SOURCE_LINKED_RECEIPT), ZERO)
        left = money(max(ZERO, line.total - (line.paid - own) - line.credited))
        if amount > left:
            raise SettlementError(
                f'{invoice.document_number}: נותרו לתשלום {money_text(left)}, '
                f'ולא ניתן לסגור בה {money_text(amount)}.'
            )

    asked = sum((amount for _, amount in wanted), ZERO)
    already = sum((f.amount for f in facts_for_payers([payer]) if f.source != SOURCE_LINKED_RECEIPT), ZERO)
    capacity = payer_capacity(payer)
    if already + asked > capacity:
        raise SettlementError(
            f'סכום החשבוניות שנסגרות ({money_text(already + asked)}) גדול מסכום '
            f'{payer.get_document_type_display()} ({money_text(capacity)}). '
            'מסמך סוגר לכל היותר את מה שהתקבל בו.'
        )
    return payer, invoices


def settle_on_issue(payer: FormalDocument, data: dict, *, user=None) -> list[DocumentSettlement]:
    """
    The settlements a document issued from the create-document form carries.

    `data['settlements']` ([{invoice_id, amount}]) when the form sent them.
    Otherwise a receipt whose linked_invoice_id (the older form's free-text
    link) names a tax invoice of kogo's, of the same customer, with something
    still open, pays it: as much of it as the receipt covers. Any other linked
    number stays as it was — text on the receipt, not a settlement.
    """
    rows = data.get('settlements') or []
    if rows:
        return record_settlements(payer, rows, user=user)
    if payer.document_type != 'receipt':
        return []
    number = ((data.get('receipt_details') or {}).get('linked_invoice_id') or '').strip()
    if not number:
        return []
    invoice = FormalDocument.objects.filter(document_number=number, document_type='tax_invoice').first()
    if invoice is None or not _same_customer(payer, invoice):
        return []
    left = balance_of(invoice).open
    amount = min(left, payer_capacity(payer))
    if amount <= 0:
        return []
    return record_settlements(payer, [{'invoice_id': invoice.pk, 'amount': amount}], user=user)


def void_settlement(settlement_id, *, user=None, reason: str = '') -> DocumentSettlement:
    """
    Void a settlement recorded by mistake. Never deleted: the row stays with
    who voided it and when, and the invoice's balance opens again by its
    amount. Voiding twice is refused (SettlementVoided).
    """
    import logging

    with transaction.atomic():
        row = (
            DocumentSettlement.objects.select_for_update(of=('self',))
            .select_related('payer', 'invoice').get(pk=settlement_id)
        )
        if row.voided_at is not None:
            raise SettlementVoided(row.voided_at)
        row.voided_at = timezone.now()
        row.voided_by = _actor(user)
        row.save(update_fields=['voided_at', 'voided_by'])
    logging.getLogger(__name__).info(
        'Settlement %s voided: %s no longer pays %s (%s) — by %s. Reason: %s',
        row.pk, row.payer.document_number, row.invoice_number, row.amount,
        getattr(user, 'email', user), (reason or '').strip() or '—',
    )
    return row


# ── Reading ──────────────────────────────────────────────────────────────────

def _day(value) -> str:
    return value.isoformat() if value else ''


def invoice_row(doc: FormalDocument, balance: Balance) -> dict:
    """An invoice with its balance, as the picker, the card and the document's detail show it."""
    return {
        'id': str(doc.pk),
        'document_number': doc.document_number,
        'document_type': doc.document_type,
        'document_type_label': doc.get_document_type_display(),
        'document_date': _day(doc.document_date),
        'due_date': _day(doc.due_date),
        'description': doc.description or '',
        **balance.as_dict(),
    }


def open_invoices(queryset, *, payer_type: str = 'receipt') -> list[dict]:
    """
    The invoices in `queryset` (already scoped to the caller and the customer)
    that a `payer_type` document can close and that still owe something —
    oldest first, as they are usually paid.
    """
    types = PAYER_FOR.get(payer_type, ())
    invoices = list(queryset.filter(document_type__in=types).order_by('document_date', 'document_number'))
    found = balances(invoices)
    return [invoice_row(doc, found[doc.pk]) for doc in invoices if found[doc.pk].open > 0]


def _line(row, *, other: FormalDocument | None, other_number: str, source: str) -> dict:
    return {
        'id': str(row.pk) if row is not None else None,
        'document_id': str(other.pk) if other is not None else None,
        'document_number': other.document_number if other is not None else other_number,
        'document_type': other.document_type if other is not None else '',
        'document_type_label': other.get_document_type_display() if other is not None else '',
        'amount': str(money(row.amount if row is not None else 0)),
        'source': source,
        'created_at': row.created_at.isoformat() if row is not None else None,
        'voided_at': row.voided_at.isoformat() if row is not None and row.voided_at else None,
        'voided_by': (row.voided_by.get_full_name() or row.voided_by.email) if row is not None and row.voided_by_id else '',
    }


def _visible(lines: list[dict], user) -> list[dict]:
    """A partner sees the lines whose other document is one of their branches' (partner_scope)."""
    from apps.documents.partner_scope import partner_branches, scope_documents

    if user is None or partner_branches(user) is None:
        return lines
    ids = [line['document_id'] for line in lines if line['document_id']]
    allowed = {str(pk) for pk in scope_documents(FormalDocument.objects.filter(pk__in=ids), user).values_list('pk', flat=True)}
    return [line for line in lines if line['document_id'] in allowed]


def document_settlements(doc: FormalDocument, *, user=None) -> dict:
    """
    What a document's detail shows: its balance when it is an invoice, what
    paid it (settled_by) and what it paid (settles) — settlement rows, voided
    ones too (with voided_at), and what an older record implies (source). For
    a partner (`user`), only the lines whose other document they may see; the
    balance is the invoice's own.
    """
    out = {'balance': None, 'settled_by': [], 'settles': []}
    if doc.document_type in INVOICE_TYPES:
        balance = balance_of(doc)
        out['balance'] = balance.as_dict()
        rows = {
            row.pk: row for row in
            DocumentSettlement.objects.filter(invoice=doc).select_related('payer', 'voided_by').order_by('created_at')
        }
        out['settled_by'] = [_line(row, other=row.payer, other_number='', source=SOURCE_SETTLEMENT) for row in rows.values()]
        implied = [fact for fact in balance.facts if fact.source != SOURCE_SETTLEMENT]
        payers = FormalDocument.objects.in_bulk([fact.payer_id for fact in implied])
        for fact in implied:
            line = _line(None, other=payers.get(fact.payer_id), other_number='', source=fact.source)
            line['amount'] = str(fact.amount)
            out['settled_by'].append(line)
    if doc.document_type in PAYER_TYPES:
        rows = DocumentSettlement.objects.filter(payer=doc).select_related('invoice', 'voided_by').order_by('created_at')
        out['settles'] = [
            _line(row, other=row.invoice, other_number=row.invoice_number, source=SOURCE_SETTLEMENT) for row in rows
        ]
        implied = [fact for fact in facts_for_payers([doc]) if fact.source != SOURCE_SETTLEMENT]
        invoices = FormalDocument.objects.in_bulk([fact.invoice_id for fact in implied])
        for fact in implied:
            line = _line(None, other=invoices.get(fact.invoice_id), other_number='', source=fact.source)
            line['amount'] = str(fact.amount)
            out['settles'].append(line)
    out['settled_by'] = _visible(out['settled_by'], user)
    out['settles'] = _visible(out['settles'], user)
    return out


def customer_balance(documents) -> dict:
    """
    A customer's balance from their issued documents (the business customer's
    card): every invoice still owing, and the sum.
    """
    invoices = [doc for doc in documents if doc.document_type in INVOICE_TYPES]
    found = balances(invoices)
    open_rows = sorted(
        (invoice_row(doc, found[doc.pk]) for doc in invoices if found[doc.pk].open > 0),
        key=lambda row: (row['document_date'], row['document_number']),
    )
    return {
        'open_total': str(sum((Decimal(row['open']) for row in open_rows), ZERO).quantize(CENT)),
        'open_count': len(open_rows),
        'paid_total': str(sum((line.paid for line in found.values()), ZERO).quantize(CENT)),
        'credited_total': str(sum((line.credited for line in found.values()), ZERO).quantize(CENT)),
        'open_invoices': open_rows,
    }


def paid_at_issue(invoice: FormalDocument) -> list[DocumentSettlement]:
    """
    The settlement rows that belong on the invoice's own page: those whose
    payer was issued no later than the invoice — a check plan's receipt paying
    the check's invoice. A receipt issued afterwards closes the invoice on the
    collections tab, but a copy printed later stays the same as its original.
    """
    moment = invoice.issued_at or invoice.created_at
    return [
        row for row in DocumentSettlement.objects.filter(invoice_id=invoice.pk).select_related('payer').order_by('created_at')
        if (row.payer.issued_at or row.payer.created_at) <= moment
    ]


def paid_on_issue(payer: FormalDocument) -> list[DocumentSettlement]:
    """
    The invoices a receipt's own page names: those issued before it. A check
    plan's receipt comes first and its invoices later — they are not on it, so
    a copy printed later stays the same as its original.
    """
    moment = payer.issued_at or payer.created_at
    return [
        row for row in DocumentSettlement.objects.filter(payer_id=payer.pk).select_related('invoice').order_by('created_at')
        if row.invoice is not None and (row.invoice.issued_at or row.invoice.created_at) <= moment
    ]
