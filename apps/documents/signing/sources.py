"""
The three kinds of issued document, seen the same way by the signing service.

kogo issues fiscal documents from three tables: lesson receipts (Invoice, the
IR run), store sales (StoreInvoice, ST/SD) and everything else (FormalDocument
— TI, IRM, RC, TX, CR, RT). Each answers the same questions here: its number,
who it is for and where their mail goes, whose consent covers it, how it was
paid, and how its original is drawn.

How it was paid decides where the original may go (הוראה 18ב(ד)): "נישום
המבקש לשלוח מסמך ממוחשב חתום בחתימה אלקטרונית מאובטחת, יקבל את התקבול בשל
הפעולה באחד מאמצעים אלה בלבד" — the customer's credit card, a check crossed
"לא סחיר" in the customer's name to the business's order, or a transfer from
the customer's account. Anything else — cash, an unmarked check, a method the
record does not name — sends the original on paper, never by mail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.utils import timezone

from apps.documents.models import DOCUMENT_TYPE_CHOICES, SignedOriginal

ALLOWED_METHODS = ('credit_card', 'bank_transfer')

REASON_CASH = 'שולם במזומן — לפי הוראה 18ב(ד) המקור נמסר ללקוח על נייר ולא במייל'
REASON_CASH_PLAN = 'מנוי במזומן — לפי הוראה 18ב(ד) המקור נמסר ללקוח על נייר ולא במייל'
REASON_CHECK_NOT_CROSSED = (
    'צ׳ק שלא סומן "משורטט, לא סחיר, על שם הלקוח" — לפי הוראה 18ב(ד) המקור נמסר על נייר'
)
REASON_UNKNOWN_METHOD = 'אמצעי התשלום לא רשום במסמך — לפי הוראה 18ב(ד) המקור נמסר על נייר'


@dataclass(frozen=True)
class PaymentVerdict:
    """Whether 18ב(ד) lets the signed original go by mail, and if not, why."""
    allowed: bool
    reason: str = ''


ALLOWED = PaymentVerdict(True)


def _local_day(moment) -> date | None:
    if moment is None:
        return None
    if timezone.is_aware(moment):
        return timezone.localtime(moment).date()
    return moment.date() if hasattr(moment, 'date') else moment


def verdict_for_payment_rows(payments) -> PaymentVerdict:
    """Every line must be an allowed means; the first one that is not decides."""
    for payment in payments:
        method = payment.payment_method
        if method in ALLOWED_METHODS:
            continue
        if method == 'check':
            if payment.check_crossed:
                continue
            return PaymentVerdict(False, REASON_CHECK_NOT_CROSSED)
        if method == 'cash':
            return PaymentVerdict(False, REASON_CASH)
        return PaymentVerdict(False, REASON_UNKNOWN_METHOD)
    return ALLOWED


class Source:
    kind: str = ''

    def __init__(self, obj):
        self.obj = obj

    @property
    def source_id(self) -> str:
        return str(self.obj.pk)

    # What every kind answers.
    number: str
    type_label: str
    customer_name: str
    document_date: date | None
    total: Decimal | None
    default_email: str
    consent_holder: object | None

    def issued(self) -> bool:
        return True

    def payment_verdict(self) -> PaymentVerdict:
        raise NotImplementedError

    def render_original(self) -> bytes:
        raise NotImplementedError

    def render_archive(self) -> bytes:
        """
        The document drawn again for the archive: "העתק לארכיון", never "מקור".

        For a document issued before signing existed, whose customer already
        holds the original (apps/documents/signing/archive.py).
        """
        raise NotImplementedError


class LessonReceiptSource(Source):
    """A lesson receipt (Invoice, IR). Charged through Tranzila, so a card, in practice."""

    kind = SignedOriginal.KIND_IR

    @property
    def number(self):
        return self.obj.invoice_number

    @property
    def type_label(self):
        from apps.core.vat import DOCUMENT_TITLE

        return DOCUMENT_TITLE

    @property
    def customer_name(self):
        family = self.obj.family if self.obj.family_id else None
        return (self.obj.payer_name or (family.name if family else '') or '').strip()

    @property
    def document_date(self):
        return _local_day(self.obj.invoice_date)

    @property
    def total(self):
        return self.obj.amount

    @property
    def default_email(self):
        family = self.obj.family if self.obj.family_id else None
        return (self.obj.payer_email or (family.email if family else '') or '').strip()

    @property
    def consent_holder(self):
        return self.obj.family if self.obj.family_id else None

    def payment_verdict(self) -> PaymentVerdict:
        method = self.obj.payment_method or ''
        if method in ALLOWED_METHODS:
            return ALLOWED
        if not method and (self.obj.tranzila_transaction_id or '').strip():
            # A charge that went through Tranzila is a card charge, even where the
            # receipt's own column was left empty.
            return ALLOWED
        if method == 'cash':
            return PaymentVerdict(False, REASON_CASH)
        if method == 'check':
            # A lesson receipt keeps no check details, so no crossing either.
            return PaymentVerdict(False, REASON_CHECK_NOT_CROSSED)
        return PaymentVerdict(False, REASON_UNKNOWN_METHOD)

    def render_original(self) -> bytes:
        from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf

        return generate_subscription_invoice_pdf(self.obj, copy=False, signed=True)

    def render_archive(self) -> bytes:
        from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf

        return generate_subscription_invoice_pdf(self.obj, archive=True)


class StoreSaleSource(Source):
    """A store sale (StoreInvoice): ST when paid, SD (חשבונית עסקה) when billed monthly."""

    kind = SignedOriginal.KIND_STORE
    ISSUED_STATUSES = ('completed', 'refunded', 'refund_failed')

    @property
    def number(self):
        return self.obj.invoice_number

    @property
    def type_label(self):
        from apps.core.vat import DOCUMENT_TITLE

        return 'חשבונית עסקה' if self.obj.payment_method == 'monthly_billing' else DOCUMENT_TITLE

    @property
    def customer_name(self):
        name = (self.obj.customer_name or '').strip()
        if not name and self.obj.child_id:
            name = self.obj.child.full_name
        return name

    @property
    def document_date(self):
        return _local_day(self.obj.issue_date)

    @property
    def total(self):
        return self.obj.total_amount

    @property
    def default_email(self):
        # The address the sale's mail goes to (apps/store/invoice_email.py).
        return (self.obj.customer_email or '').strip()

    @property
    def consent_holder(self):
        child = self.obj.child if self.obj.child_id else None
        return getattr(child, 'family', None) if child else None

    def issued(self) -> bool:
        return self.obj.payment_method == 'monthly_billing' or self.obj.payment_status in self.ISSUED_STATUSES

    def payment_verdict(self) -> PaymentVerdict:
        method = self.obj.payment_method
        if method == 'credit_card':
            return ALLOWED
        if method == 'monthly_billing':
            # A חשבונית עסקה records no money; the month's charge, on the card of
            # the standing order, gets its own receipt.
            return ALLOWED
        if method == 'cash':
            return PaymentVerdict(False, REASON_CASH)
        return PaymentVerdict(False, REASON_UNKNOWN_METHOD)

    def render_original(self) -> bytes:
        from apps.store.invoice_pdf import generate_store_invoice_pdf

        return generate_store_invoice_pdf(self.obj, copy=False, signed=True)

    def render_archive(self) -> bytes:
        from apps.store.invoice_pdf import generate_store_invoice_pdf

        return generate_store_invoice_pdf(self.obj, archive=True)


class FormalDocumentSource(Source):
    """A FormalDocument: issued by hand, by a cash or check plan, for a refund, or for a rent charge."""

    kind = SignedOriginal.KIND_FORMAL
    LABELS = dict(DOCUMENT_TYPE_CHOICES)

    @property
    def number(self):
        return self.obj.document_number

    @property
    def type_label(self):
        return self.LABELS.get(self.obj.document_type, self.obj.document_type)

    @property
    def customer_name(self):
        doc = self.obj
        if doc.business_customer_id:
            return doc.business_customer.full_name
        if doc.child_id:
            return doc.child.full_name
        return (doc.customer_name or '').strip()

    @property
    def document_date(self):
        return self.obj.document_date

    @property
    def total(self):
        return self.obj.total_amount

    @property
    def default_email(self):
        doc = self.obj
        if doc.business_customer_id:
            return (doc.business_customer.email or '').strip()
        if doc.child_id:
            family = getattr(doc.child, 'family', None)
            return (family.email or '').strip() if family else ''
        return ''

    @property
    def consent_holder(self):
        doc = self.obj
        if doc.business_customer_id:
            return doc.business_customer
        if doc.child_id:
            return getattr(doc.child, 'family', None)
        return None

    def issued(self) -> bool:
        return self.obj.document_type != 'draft'

    def payment_verdict(self) -> PaymentVerdict:
        doc = self.obj
        # A cash plan's monthly document carries no payment line: the money was
        # the cash taken at registration (apps/documents/cash_plans.py).
        if doc.cash_plan_month.exists():
            return PaymentVerdict(False, REASON_CASH_PLAN)
        # A check plan's monthly invoice is paid by the check of that month; the
        # checks are on the plan's receipt.
        item = doc.check_item_invoices.select_related('plan__receipt').first()
        if item is not None:
            receipt = item.plan.receipt
            if receipt is None:
                return PaymentVerdict(False, REASON_UNKNOWN_METHOD)
            return verdict_for_payment_rows(receipt.payments.all()) if receipt.payments.exists() \
                else PaymentVerdict(False, REASON_UNKNOWN_METHOD)
        payments = list(doc.payments.all())
        if payments:
            return verdict_for_payment_rows(payments)
        if doc.document_type in ('receipt', 'combined'):
            # A receipt that names no means of payment cannot show it was an allowed one.
            return PaymentVerdict(False, REASON_UNKNOWN_METHOD)
        # A tax invoice, a transaction invoice or a credit note receives no money itself.
        return ALLOWED

    def render_original(self) -> bytes:
        from apps.documents.document_pdf import generate_document_pdf

        return generate_document_pdf(self.obj, signed=True)

    def render_archive(self) -> bytes:
        from apps.documents.document_pdf import generate_document_pdf

        return generate_document_pdf(self.obj, archive=True)


SOURCES = {
    SignedOriginal.KIND_IR: LessonReceiptSource,
    SignedOriginal.KIND_STORE: StoreSaleSource,
    SignedOriginal.KIND_FORMAL: FormalDocumentSource,
}


def source_for(kind: str, obj) -> Source:
    return SOURCES[kind](obj)


def load_source(kind: str, source_id) -> Source:
    """The issuing row, fresh from the database."""
    if kind == SignedOriginal.KIND_IR:
        from apps.customers.financial_models import Invoice

        obj = Invoice.objects.select_related('family').get(pk=source_id)
    elif kind == SignedOriginal.KIND_STORE:
        from apps.store.models import StoreInvoice

        obj = StoreInvoice.objects.select_related('child', 'child__family').get(pk=source_id)
    elif kind == SignedOriginal.KIND_FORMAL:
        from apps.documents.models import FormalDocument

        obj = FormalDocument.objects.select_related(
            'business_customer', 'child', 'child__family', 'linked_document',
        ).get(pk=source_id)
    else:
        raise ValueError(f'Unknown kind {kind!r}')
    return source_for(kind, obj)
