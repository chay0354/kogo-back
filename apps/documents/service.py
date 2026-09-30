import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from django.utils import timezone
from django.db import transaction

from apps.documents.models import (
    FormalDocument, DocumentLineItem, DocumentPayment,
    TRANZILA_DOCUMENT_TYPE,
)
from apps.documents.numbering import israel_today, validate_document_date

logger = logging.getLogger(__name__)

VAT_RATE = Decimal('0.18')
# To the agora, half up — the rounding apps.core.vat applies to every other
# document. Decimal's default (half-even) put ₪18.045 of VAT at ₪18.04 here
# and at ₪18.05 on a lesson receipt.
AGORA = Decimal('0.01')


DRAFT_TYPE = 'draft'
# What a draft may become. A receipt and an invoice-receipt (owner, 25.9) carry
# their payment rows from the draft on, and the invoices they will settle in
# draft_settlements — both checked again when the draft is approved.
DRAFT_TARGET_TYPES = ('tax_invoice', 'transaction_invoice', 'receipt', 'combined')
# The draft targets that receive money: they settle invoices and are paid in rows.
PAYING_DRAFT_TARGETS = ('receipt', 'combined')


def _generate_draft_number() -> str:
    """Drafts never take a fiscal number, so a discarded draft leaves no gap."""
    return f"D-{uuid.uuid4().hex[:8].upper()}"


def _branch_for(data: dict):
    """A private client's document belongs to the family's branch unless one was chosen."""
    if data.get('branch_id') or not data.get('child_id'):
        return data.get('branch_id')
    from apps.customers.models import Child
    child = Child.objects.select_related('family').filter(pk=data['child_id']).only('family__branch_id').first()
    return child.family.branch_id if child and child.family_id else None


def _income_tags(data: dict) -> dict:
    """The business and category a document's income belongs to: as given, else the business customer's."""
    business_id = data.get('business_id')
    category_id = data.get('business_category_id')
    if not business_id and data.get('business_customer_id'):
        from apps.customers.models import BusinessCustomer
        customer = BusinessCustomer.objects.filter(pk=data['business_customer_id']).only('business_id', 'business_category_id').first()
        if customer:
            business_id = customer.business_id
            category_id = category_id or customer.business_category_id
    return {'business_id': business_id, 'business_category_id': category_id}


def _actor(user):
    """The user a document is recorded as issued by — None for a system run or an anonymous caller."""
    return user if getattr(user, 'is_authenticated', False) else None


def _issued(issued_by) -> dict:
    """
    When and by whom a document was issued (FormalDocument.issued_at/issued_by).

    The page's "תאריך ושעה" and the uniform file's 1205/1206 read issued_at:
    the moment the document took its number, which for an approved draft is the
    approval, not the day it was typed.
    """
    return {'issued_at': timezone.now(), 'issued_by': _actor(issued_by)}


def _allocation_at_issue(invoice_data: dict, issued_by) -> dict:
    """
    מספר הקצאה typed with the document (B), so the original carries it from
    the first print — a number added later is only ever on a copy. Checked to
    nine digits by the serializer; entered by the issuing user, now.
    """
    number = (invoice_data.get('allocation_number') or '').strip()
    if not number:
        return {}
    return {
        'allocation_number': number,
        'allocation_entered_at': timezone.now(),
        'allocation_entered_by': _actor(issued_by),
    }


def _generate_document_number(document_type: str) -> str:
    """The next number in the run of the document's type (numbering.FORMAL_SERIES, סעיף 5(ג))."""
    from apps.documents.numbering import formal_document_number
    return formal_document_number(document_type)


def _number_and_date(document_type: str, document_date):
    """
    (the next number of the type's run, the date the document carries).

    The number is taken first — DocumentSeries.next_number holds the run's row
    lock until the transaction ends — and the date is checked against the run
    after it (numbering.validate_document_date: not in the future, in this tax
    year, not before the run's latest date). Two documents issued at the same
    moment are so checked one after the other, and a refused date rolls its
    number back with the rest of the transaction: no gap.

    Every caller goes through the rules. The check and cash plans used to be
    let past them (their documents were dated on the check's or the month's
    day); since WS-3 (30.9.2026) they are dated the day they are issued.
    """
    number = _generate_document_number(document_type)
    return number, validate_document_date(document_type, document_date)


def _sign_at_issue(doc: FormalDocument, **delivery) -> None:
    """
    Record the document's signed original, to be signed after the commit
    (apps/documents/signing). A no-op while DOCUMENT_SIGNING_ENABLED is off;
    never raises into the document's own transaction.
    """
    from apps.documents.signing.service import KIND_FORMAL, issue

    issue(KIND_FORMAL, doc, **delivery)


def _compute_totals(line_items: list, discount_amount: Decimal, discount_percent: Decimal,
                    vat_exempt: bool, prices_include_vat: bool = False) -> dict:
    """
    Subtotal, discount, VAT and total — each to the agora, half up, as printed
    and stored (the dialog works them out the same way, utils.computeInvoiceTotals).

    There is no rounding of the total to the shekel any more ("עגל סכום",
    D6): it moved the total off its net plus VAT.
    """
    subtotal = sum(
        (Decimal(str(i['quantity'])) * Decimal(str(i['price'])) for i in line_items), Decimal('0'),
    ).quantize(AGORA, rounding=ROUND_HALF_UP)
    if discount_amount < 0 or discount_percent < 0 or discount_percent > 100:
        raise ValueError('הנחה בין 0 ל־100 אחוז, ובסכום שאינו שלילי')
    effective_discount = (
        discount_amount if discount_amount > 0 else subtotal * discount_percent / 100
    ).quantize(AGORA, rounding=ROUND_HALF_UP)
    if effective_discount > subtotal:
        raise ValueError('ההנחה גדולה מסכום השורות')
    base = subtotal - effective_discount
    if prices_include_vat and not vat_exempt:
        # The prices are gross: the total is what was paid, VAT is taken out of it.
        net = (base / (1 + VAT_RATE)).quantize(AGORA, rounding=ROUND_HALF_UP)
        vat = base - net
        total = base
        subtotal = subtotal - vat
    else:
        vat = Decimal('0') if vat_exempt else (base * VAT_RATE).quantize(AGORA, rounding=ROUND_HALF_UP)
        total = base + vat
    return {
        'subtotal': subtotal,
        'discount_amount': effective_discount,
        'vat_amount': vat,
        'total_amount': total,
    }


@transaction.atomic
def create_invoice(data: dict, document_type: str, *, issued_by=None) -> FormalDocument:
    """Create a tax invoice or transaction invoice."""
    invoice_data = data['invoice_details']
    # A חשבונית עסקה is a demand for payment: it shows the VAT the tax invoice
    # issued with the payment will charge, so the customer is asked for the
    # whole sum. It is VAT-free only when the sale is (Eilat, abroad) — it
    # used to be forced exempt, and printed "פטור" on a taxable sale.
    vat_exempt = invoice_data.get('vat_exempt', False)
    totals = _compute_totals(
        invoice_data['line_items'],
        Decimal(str(invoice_data.get('discount_amount', 0))),
        Decimal(str(invoice_data.get('discount_percent', 0))),
        vat_exempt,
        invoice_data.get('prices_include_vat', False),
    )

    number, document_date = _number_and_date(document_type, invoice_data['document_date'])
    doc = FormalDocument.objects.create(
        document_number=number,
        document_type=document_type,
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        **_income_tags(data),
        branch_id=_branch_for(data),
        document_date=document_date,
        due_date=invoice_data.get('due_date') or None,
        description=invoice_data.get('description', ''),
        currency=invoice_data.get('currency', 'ILS'),
        prices_include_vat=invoice_data.get('prices_include_vat', False),
        payment_terms=invoice_data.get('payment_terms', ''),
        vat_exempt=vat_exempt,
        vat_percent=Decimal('18'),
        customer_notes=invoice_data.get('customer_notes', ''),
        internal_notes=invoice_data.get('internal_notes', ''),
        **totals,
        **_issued(issued_by),
        **_allocation_at_issue(invoice_data, issued_by),
    )

    for item in invoice_data['line_items']:
        DocumentLineItem.objects.create(
            document=doc,
            sku=item.get('sku', ''),
            description=item.get('description', ''),
            quantity=Decimal(str(item.get('quantity', 1))),
            unit_price=Decimal(str(item.get('price', 0))),
        )

    _attempt_tranzila(doc)
    _sign_at_issue(doc)
    return doc


def _create_line_items(doc: FormalDocument, line_items: list) -> None:
    for item in line_items:
        DocumentLineItem.objects.create(
            document=doc,
            sku=item.get('sku', ''),
            description=item.get('description', ''),
            quantity=Decimal(str(item.get('quantity', 1))),
            unit_price=Decimal(str(item.get('price', 0))),
        )


@transaction.atomic
def create_draft(data: dict) -> FormalDocument:
    """
    Save a document as a draft: no fiscal number, never signed, nothing sent
    to Tranzila, in no report, export or run.

    A tax invoice or a transaction invoice keeps its lines. A receipt or an
    invoice-receipt keeps its payment rows too — built and checked exactly as
    when it is issued directly — and the invoices it will settle, checked
    against today's balances (so the office hears now that one is closed) and
    kept in draft_settlements: a draft pays nothing until it is approved.
    """
    target = data.get('draft_target_type') or 'tax_invoice'
    if target not in DRAFT_TARGET_TYPES:
        raise ValueError(f'לא ניתן לשמור טיוטה לסוג {target}')
    if target == 'receipt':
        return _create_receipt_draft(data)
    if target == 'combined':
        return _create_combined_draft(data)
    invoice_data = data['invoice_details']
    vat_exempt = invoice_data.get('vat_exempt', False)
    totals = _compute_totals(
        invoice_data['line_items'],
        Decimal(str(invoice_data.get('discount_amount', 0))),
        Decimal(str(invoice_data.get('discount_percent', 0))),
        vat_exempt,
        invoice_data.get('prices_include_vat', False),
    )
    doc = FormalDocument.objects.create(
        document_number=_generate_draft_number(),
        document_type=DRAFT_TYPE,
        draft_target_type=target,
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        **_income_tags(data),
        branch_id=_branch_for(data),
        document_date=invoice_data['document_date'],
        due_date=invoice_data.get('due_date') or None,
        description=invoice_data.get('description', ''),
        currency=invoice_data.get('currency', 'ILS'),
        prices_include_vat=invoice_data.get('prices_include_vat', False),
        payment_terms=invoice_data.get('payment_terms', ''),
        vat_exempt=vat_exempt,
        vat_percent=Decimal('18'),
        customer_notes=invoice_data.get('customer_notes', ''),
        internal_notes=invoice_data.get('internal_notes', ''),
        **totals,
    )
    for item in invoice_data['line_items']:
        DocumentLineItem.objects.create(
            document=doc,
            sku=item.get('sku', ''),
            description=item.get('description', ''),
            quantity=Decimal(str(item.get('quantity', 1))),
            unit_price=Decimal(str(item.get('price', 0))),
        )
    return doc


def _draft_settlements(doc: FormalDocument, target: str, data: dict):
    """The settlements a paying draft carries: checked now, written only when it is approved."""
    from apps.documents.settlement import check_draft_settlements

    rows = check_draft_settlements(doc, target, data.get('settlements') or [])
    return rows or None


def _create_receipt_draft(data: dict) -> FormalDocument:
    """A receipt's draft: its amount, payment rows and settlements, as create_receipt builds them."""
    receipt = data['receipt_details']
    if receipt.get('invoice_per_check'):
        # A check plan opens with the receipt, and its invoices are issued on
        # the checks' days — nothing a draft can wait with.
        raise ValueError(
            "חשבונית מס לכל צ'ק לא נשמרת בטיוטה — היא פותחת תוכנית צ'קים עם הקבלה. "
            'הפיקו את הקבלה עצמה, או שמרו טיוטה בלי הסימון.'
        )
    fields = _receipt_fields(data)
    rows = _receipt_payment_rows(receipt, fields['total_amount'])
    _check_paid_in_rows('receipt', fields['total_amount'], fields['withholding_amount'], rows)
    doc = FormalDocument.objects.create(
        document_number=_generate_draft_number(),
        document_type=DRAFT_TYPE,
        draft_target_type='receipt',
        # Typed for reference only: an approved draft is dated the day it is approved.
        document_date=data.get('document_date') or israel_today(),
        **fields,
    )
    for row in rows:
        DocumentPayment.objects.create(document=doc, **row)
    doc.draft_settlements = _draft_settlements(doc, 'receipt', data)
    doc.save(update_fields=['draft_settlements'])
    return doc


def _create_combined_draft(data: dict) -> FormalDocument:
    """An invoice-receipt's draft: its lines, totals, payment rows and settlements, as create_combined builds them."""
    invoice_data = data['invoice_details']
    fields = _combined_fields(data)
    rows = _combined_payment_rows(data, invoice_data, fields['total_amount'])
    _check_paid_in_rows('combined', fields['total_amount'], fields['withholding_amount'], rows)
    doc = FormalDocument.objects.create(
        document_number=_generate_draft_number(),
        document_type=DRAFT_TYPE,
        draft_target_type='combined',
        document_date=invoice_data['document_date'],
        **fields,
    )
    _create_line_items(doc, invoice_data['line_items'])
    for row in rows:
        DocumentPayment.objects.create(document=doc, **row)
    doc.draft_settlements = _draft_settlements(doc, 'combined', data)
    doc.save(update_fields=['draft_settlements'])
    return doc


def _check_paid_in_rows(target: str, total: Decimal, withheld, rows) -> None:
    """
    A receipt or an invoice-receipt is paid in rows of its own amounts: at
    least one, each above zero. A receipt's rows are what it received (its
    total; ניכוי במקור is kept beside it); an invoice-receipt's rows and the
    withholding come to its total exactly (G). Checked when a draft is saved
    and again when it is approved, on the rows it carries.
    """
    amounts = [Decimal(str(row['amount'])) for row in rows]
    if not amounts or any(amount <= 0 for amount in amounts) or total <= 0:
        raise ValueError('למסמך שמקבל תשלום צריך לפחות אמצעי תשלום אחד, בסכום גדול מאפס')
    paid = sum(amounts, Decimal('0'))
    due = total if target == 'receipt' else total - (withheld or Decimal('0'))
    if paid != due:
        raise ValueError(
            f'סכומי אמצעי התשלום ({_money_text(paid)}) אינם שווים לסכום המסמך ({_money_text(due)}). '
            'כל שקל שהתקבל נרשם פעם אחת, באמצעי שבו שולם.'
        )


def _settle_approved_draft(doc: FormalDocument, pending, issued_by) -> list:
    """
    The invoices an approved receipt or invoice-receipt settles, recorded
    through settle_on_issue — the call a directly issued document goes
    through — against the balances of now: a receipt or a credit note may have
    closed one of them since the draft was saved. A refusal (SettlementError,
    a ValueError) rolls the approval back, number and all.
    """
    from apps.documents.settlement import settle_on_issue

    data = {
        'settlements': [{'invoice_id': row.get('invoice_id'), 'amount': row.get('amount')} for row in pending or []],
        # The older form's free-text link, kept on the draft as on a receipt.
        'receipt_details': {'linked_invoice_id': doc.linked_document_number or ''},
    }
    return settle_on_issue(doc, data, user=issued_by)


@transaction.atomic
def finalize_draft(doc: FormalDocument, *, issued_by=None) -> FormalDocument:
    """
    Approve a draft: it becomes its target type, takes the next fiscal number,
    and is dated and stamped the day and moment it is approved.

    The row is locked before it is looked at. Checked first on the caller's
    copy, two approvals of the same draft both saw a draft: the second waited
    for the first's lock and then numbered the now-issued document again —
    leaving its first number a gap in the run and a second signed original.
    Read under the lock, the second finds a document that is no longer a draft.

    The date is today in Israel, not the day the draft was typed: a document
    is dated when it is issued (הוראה 17), and a draft kept for a week would
    otherwise take a number after documents dated later than it.

    A receipt or an invoice-receipt is checked again before its number: its
    payment rows still add up (_check_paid_in_rows), and the invoices it
    settles still owe what it pays (settlement.record_settlements, under the
    invoices' locks). Any refusal rolls the approval back and uses no number.
    Signing and delivery are the direct issue's: the same _sign_at_issue on
    the same document, whose payment rows decide mail or paper.
    """
    doc = FormalDocument.objects.select_for_update().get(pk=doc.pk)
    if doc.document_type != DRAFT_TYPE:
        raise ValueError('המסמך אינו טיוטה')
    target = doc.draft_target_type or 'tax_invoice'
    if target not in DRAFT_TARGET_TYPES:
        raise ValueError(f'סוג יעד לא נתמך: {target}')
    paying = target in PAYING_DRAFT_TARGETS
    if paying:
        _check_paid_in_rows(
            target, doc.total_amount, doc.withholding_amount,
            [{'amount': payment.amount} for payment in doc.payments.all()],
        )
    pending = doc.draft_settlements
    doc.document_type = target
    doc.document_number, doc.document_date = _number_and_date(target, israel_today())
    issued = _issued(issued_by)
    doc.issued_at = issued['issued_at']
    doc.issued_by = issued['issued_by']
    # From here the settlement rows are the record of what it pays.
    doc.draft_settlements = None
    doc.save(update_fields=[
        'document_type', 'document_number', 'document_date', 'issued_at', 'issued_by',
        'draft_settlements', 'updated_at',
    ])
    if paying:
        # Before anything leaves the system (Tranzila, the signed original):
        # a settlement refused here rolls back a document nobody saw.
        _settle_approved_draft(doc, pending, issued_by)
    _attempt_tranzila(doc)
    _sign_at_issue(doc)
    return doc


@transaction.atomic
def discard_draft(doc: FormalDocument) -> str:
    """
    Delete a draft. It never took a number, was never signed and settled
    nothing, so nothing is left behind: no gap in any run, no original, no
    settlement (its payment rows and lines go with it). Anything no longer a
    draft is refused — an issued document is never deleted (סעיף 23(ב)).
    """
    doc = FormalDocument.objects.select_for_update().get(pk=doc.pk)
    if doc.document_type != DRAFT_TYPE:
        raise ValueError('רק טיוטה נמחקת. מסמך שהונפק מתוקן בחשבונית זיכוי.')
    number = doc.document_number
    doc.delete()
    return number


def _combined_fields(data: dict) -> dict:
    """
    What an invoice-receipt records besides its number, date, stamp and
    allocation number — its customer, tags, terms and totals. Shared by
    create_combined and an invoice-receipt's draft, so a draft is approved as
    exactly what a direct issue would have written.
    """
    invoice_data = data['invoice_details']
    totals = _compute_totals(
        invoice_data['line_items'],
        Decimal(str(invoice_data.get('discount_amount', 0))),
        Decimal(str(invoice_data.get('discount_percent', 0))),
        invoice_data.get('vat_exempt', False),
        invoice_data.get('prices_include_vat', False),
    )
    return {
        'client_type': data['client_type'],
        'child_id': data.get('child_id'),
        'business_customer_id': data.get('business_customer_id'),
        **_income_tags(data),
        'branch_id': _branch_for(data),
        'due_date': invoice_data.get('due_date') or None,
        'description': invoice_data.get('description', ''),
        'currency': invoice_data.get('currency', 'ILS'),
        'prices_include_vat': invoice_data.get('prices_include_vat', False),
        'payment_terms': invoice_data.get('payment_terms', ''),
        'vat_exempt': invoice_data.get('vat_exempt', False),
        'vat_percent': Decimal('18'),
        'customer_notes': invoice_data.get('customer_notes', ''),
        'internal_notes': invoice_data.get('internal_notes', ''),
        'withholding_amount': _withholding(invoice_data.get('withholding_amount')),
        **totals,
    }


@transaction.atomic
def create_combined(data: dict, *, issued_by=None) -> FormalDocument:
    """Create a combined tax invoice + receipt."""
    invoice_data = data['invoice_details']
    fields = _combined_fields(data)
    # How it was paid is checked before the number is taken (a refusal used to
    # come after it, and roll it back with the rest).
    payments = _combined_payment_rows(data, invoice_data, fields['total_amount'])

    number, document_date = _number_and_date('combined', invoice_data['document_date'])
    doc = FormalDocument.objects.create(
        document_number=number,
        document_type='combined',
        document_date=document_date,
        **fields,
        **_issued(issued_by),
        **_allocation_at_issue(invoice_data, issued_by),
    )

    _create_line_items(doc, invoice_data['line_items'])
    for row in payments:
        DocumentPayment.objects.create(document=doc, **row)

    _attempt_tranzila(doc)
    _sign_at_issue(doc)
    return doc


def _receipt_fields(data: dict) -> dict:
    """
    What a receipt records besides its number, date and stamp: its customer,
    tags, the amount it received and the ניכוי במקור beside it. Shared by
    create_receipt and a receipt's draft.
    """
    receipt = data['receipt_details']
    amount = _receipt_amount(receipt)
    return {
        'client_type': data['client_type'],
        'child_id': data.get('child_id'),
        'business_customer_id': data.get('business_customer_id'),
        **_income_tags(data),
        'branch_id': _branch_for(data),
        'currency': 'ILS',
        'vat_exempt': True,
        'vat_percent': Decimal('18'),
        'subtotal': amount,
        'discount_amount': Decimal('0'),
        'discount_percent': Decimal('0'),
        'vat_amount': Decimal('0'),
        'total_amount': amount,
        'linked_document_number': receipt.get('linked_invoice_id', ''),
        'customer_notes': (
            receipt.get('check_notes', '') or receipt.get('cash_notes', '')
            or receipt.get('bank_notes', '') or receipt.get('card_notes', '')
        ),
        # ניכוי במקור the customer withheld — it was read and dropped before.
        'withholding_amount': _withholding(receipt.get('withholding')),
    }


def _receipt_payment_rows(receipt: dict, amount: Decimal) -> list[dict]:
    """A receipt's payment rows (DocumentPayment fields): one per confirmed check, else one for the amount."""
    method_key = _map_payment_method(receipt['payment_method'])
    if method_key == 'check':
        confirmed = [c for c in receipt.get('checks', []) if c.get('confirmed') and c.get('amount', 0) > 0]
        return [
            {
                'payment_method': 'check',
                'amount': Decimal(str(chk['amount'])),
                'reference': chk.get('check_number', ''),
                'check_date': chk.get('date') or None,
                'check_bank': chk.get('bank', ''),
                'check_branch': chk.get('branch', ''),
                'check_account': chk.get('account_number', ''),
                # הוראה 18ב(ד)(2): only a check crossed "לא סחיר", in the
                # customer's name, lets the signed receipt go by mail.
                'check_crossed': chk.get('check_crossed') is True,
            }
            for chk in confirmed
        ]
    if method_key == 'credit_card':
        return [{
            'payment_method': 'credit_card',
            'amount': Decimal(str(receipt.get('card_amount', 0))),
            'card_last_four': receipt.get('card_last_four', ''),
            'card_brand': (receipt.get('card_brand') or '').strip() or None,
            'card_expiry': receipt.get('card_expiry', ''),
            'card_installments': receipt.get('card_installments', 1),
            'notes': receipt.get('card_notes', ''),
        }]
    if method_key == 'bank_transfer':
        return [{
            'payment_method': 'bank_transfer',
            'amount': Decimal(str(receipt.get('bank_amount', 0))),
            'reference': receipt.get('bank_reference', ''),
            'paid_on': receipt.get('bank_date') or None,
            'notes': receipt.get('bank_notes', ''),
        }]
    return [{'payment_method': method_key, 'amount': amount, 'notes': receipt.get('cash_notes', '')}]


@transaction.atomic
def create_receipt(data: dict, *, issued_by=None) -> FormalDocument:
    """Create a standalone receipt."""
    receipt = data['receipt_details']
    fields = _receipt_fields(data)
    payments = _receipt_payment_rows(receipt, fields['total_amount'])

    # Today in Israel when no date is given — the server's UTC date was the
    # previous day for the first two or three hours of every Israeli morning.
    number, document_date = _number_and_date('receipt', data.get('document_date') or israel_today())
    doc = FormalDocument.objects.create(
        document_number=number,
        document_type='receipt',
        document_date=document_date,
        **fields,
        **_issued(issued_by),
    )
    for row in payments:
        DocumentPayment.objects.create(document=doc, **row)

    _attempt_tranzila(doc)
    _sign_at_issue(doc)
    return doc


@transaction.atomic
def create_credit_invoice(data: dict, *, issued_by=None) -> FormalDocument:
    """Create a credit note (חשבונית מס זיכוי)."""
    credit = data['credit_invoice_details']
    amount_before_vat = Decimal(str(credit['credit_amount_before_vat']))
    vat_exempt = credit.get('vat_exempt', False)
    vat_amount = Decimal('0') if vat_exempt else (amount_before_vat * VAT_RATE).quantize(AGORA, rounding=ROUND_HALF_UP)
    total = amount_before_vat + vat_amount

    if amount_before_vat <= 0:
        raise ValueError('סכום הזיכוי חייב להיות גדול מאפס')
    linked_number = (credit.get('linked_invoice_id') or '').strip()
    # What it credits, when kogo issued it: checked (type, customer, what is
    # left to credit) under a lock on the original, so two credits at once
    # cannot together pass its total.
    linked_doc = _check_creditable(linked_number, data, amount_before_vat)
    # The original's date is printed beside its number (סעיף 9(ה)(4)): kogo's
    # own record of it when kogo issued it — a date typed beside a number kogo
    # knows is not trusted over the document — else as typed (the previous
    # software's number).
    linked_date = original_document_date(linked_number) or credit.get('linked_document_date')

    number, document_date = _number_and_date('credit_invoice', credit['document_date'])
    doc = FormalDocument.objects.create(
        document_number=number,
        document_type='credit_invoice',
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        **_income_tags(data),
        branch_id=_branch_for(data),
        document_date=document_date,
        vat_exempt=vat_exempt,
        vat_percent=Decimal('18'),
        subtotal=amount_before_vat,
        discount_amount=Decimal('0'),
        discount_percent=Decimal('0'),
        vat_amount=vat_amount,
        total_amount=total,
        linked_document=linked_doc,
        linked_document_number=linked_number,
        linked_document_date=linked_date,
        credit_reason=credit.get('credit_reason', ''),
        customer_notes=credit.get('customer_notes', ''),
        internal_notes=credit.get('internal_notes', ''),
        **_issued(issued_by),
    )

    _attempt_tranzila(doc)

    from apps.documents import signing

    if signing.enabled():
        # Signed and mailed after the commit, never from inside it: mailed
        # before, a transaction that then rolled back left the customer holding
        # a number the run hands out again — and a retry mailed it twice. The
        # signed original's row is the sent flag (SignedOriginal.sent_at).
        from apps.documents.models import SignedOriginal

        name, email = _credit_note_recipient(doc)
        _sign_at_issue(doc, channel=SignedOriginal.CHANNEL_CREDIT_NOTE, email_to=email, customer_name=name)
        doc_id = doc.pk
        transaction.on_commit(lambda: _email_credit_note_after_commit(doc_id))
        return doc

    # A credit note is only useful to the customer if it reaches them. Never let a
    # mail failure roll back a document that was already issued and numbered.
    try:
        _email_credit_note(doc)
    except Exception:
        logger.exception('Credit note email failed for %s (non-fatal)', doc.document_number)

    return doc


def record_customer_ack(doc: FormalDocument, note: str, on=None) -> FormalDocument:
    """
    Record that the customer confirmed receiving a credit note (הוראה 23א(3)):
    the credit reduces the VAT only once they have. `note` says how — a
    signature on the copy, registered mail, a signed reply. Recorded once.

    `on` is the day the confirmation arrived, when the office records it later
    (a signed copy that came back last week): not in the future, not before
    the credit note itself. Without it — now. A past day is kept at noon,
    Israel time, so it reads as that day in every time zone the system prints.
    """
    note = (note or '').strip()
    if doc.document_type != 'credit_invoice':
        raise ValueError('אישור לקוח נרשם על חשבונית זיכוי בלבד')
    if not note:
        raise ValueError('יש לציין איך הלקוח אישר את קבלת הזיכוי (חתימה על העתק, דואר רשום, תשובה חתומה)')
    moment = timezone.now()
    if on is not None:
        today = israel_today()
        if on > today:
            raise ValueError('תאריך האישור אינו יכול להיות בעתיד')
        if doc.document_date and on < doc.document_date:
            raise ValueError(
                f'תאריך האישור ({on:%d/%m/%Y}) מוקדם מתאריך חשבונית הזיכוי ({doc.document_date:%d/%m/%Y})'
            )
        if on != today:
            from datetime import datetime, time
            from zoneinfo import ZoneInfo

            moment = datetime.combine(on, time(12, 0), tzinfo=ZoneInfo('Asia/Jerusalem'))
    with transaction.atomic():
        locked = FormalDocument.objects.select_for_update().get(pk=doc.pk)
        if locked.customer_ack_at is not None:
            raise AlreadyAcknowledged(locked.customer_ack_at)
        locked.customer_ack_at = moment
        locked.customer_ack_note = note[:300]
        locked.save(update_fields=['customer_ack_at', 'customer_ack_note', 'updated_at'])
    return locked


class AlreadyAcknowledged(Exception):
    """The customer's confirmation of a credit note is on record already — it is not written over."""

    def __init__(self, at):
        super().__init__(at)
        self.at = at


def _email_credit_note_after_commit(doc_id, *, customer_name: str | None = None, email: str | None = None) -> None:
    """The credit note's mail, after its document committed. Its failure is logged, never raised."""
    doc = FormalDocument.objects.select_related('business_customer', 'child__family', 'linked_document').get(pk=doc_id)
    try:
        _email_credit_note(doc, customer_name=customer_name, email=email)
    except Exception:
        logger.exception('Credit note email failed for %s (non-fatal; the signing cron retries)', doc.document_number)


# What a credit note may credit (הוראה 23א, סעיף 9(ה)): a tax invoice, or a
# tax invoice-receipt. Not a receipt (no VAT was charged on it), not a
# transaction invoice (a demand for payment, not a tax document), not a draft
# (no number yet), and not another credit note.
CREDITABLE_TYPES = ('tax_invoice', 'combined')


class CreditRoom:
    """
    What is left to credit of the document `number` ("נותר לזכות"), before VAT.

    known     — kogo issued it (by hand, a lesson receipt IR, a store sale ST);
                False for the previous software's numbers, whose amount kogo
                does not know (their date vouches for them, WS-2 I).
    refusal   — why it cannot be credited at all ('' when it can): not a tax
                document, a store demand for payment, a charge that never
                completed.
    net, credited, left — before VAT; None when not known or refused.
    """

    def __init__(self, number: str, *, known: bool = False, kind: str = '', original=None,
                 document_type: str = '', document_type_label: str = '', document_date=None,
                 net=None, credited=Decimal('0'), refusal: str = ''):
        self.number = number
        self.known = known
        self.kind = kind
        self.original = original
        self.document_type = document_type
        self.document_type_label = document_type_label
        self.document_date = document_date
        self.net = net
        self.credited = credited
        self.refusal = refusal

    @property
    def left(self):
        return None if self.net is None else self.net - self.credited

    def as_dict(self) -> dict:
        money = (lambda value: None if value is None else str(Decimal(value).quantize(AGORA)))
        return {
            'number': self.number,
            'known': self.known,
            'kind': self.kind,
            'document_type': self.document_type,
            'document_type_label': self.document_type_label,
            'document_date': self.document_date.isoformat() if self.document_date else None,
            'creditable': self.known and not self.refusal,
            'refusal': self.refusal,
            'net': money(self.net),
            'credited': money(self.credited) if self.known else None,
            # Never below zero on the screen; the rule compares the true figure.
            'left': money(max(self.left, Decimal('0'))) if self.left is not None else None,
            'child_id': str(self.original.child_id) if self.original is not None and self.original.child_id else None,
            'business_customer_id': (
                str(self.original.business_customer_id)
                if self.original is not None and self.original.business_customer_id else None
            ),
        }


def credit_room(number: str, *, lock: bool = False) -> CreditRoom:
    """
    How much of `number` is left to credit, before VAT: its amount less the
    credit notes already issued against it (by its row, or by its number).
    With `lock` the original's row is locked first, so two credit notes at
    once are checked one after the other (_check_creditable).
    """
    from django.db.models import Q, Sum

    from apps.core.vat import split_vat_inclusive
    from apps.customers.financial_models import Invoice
    from apps.store.models import StoreInvoice

    number = (number or '').strip()
    if not number:
        return CreditRoom(number)
    formal = FormalDocument.objects.select_for_update() if lock else FormalDocument.objects
    original = formal.filter(document_number=number).first()
    if original is not None:
        room = CreditRoom(
            number, known=True, kind='formal', original=original,
            document_type=original.document_type, document_type_label=original.get_document_type_display(),
            document_date=original.document_date,
        )
        if original.document_type not in CREDITABLE_TYPES:
            room.refusal = (
                f'{number} הוא {room.document_type_label}. חשבונית זיכוי מזכה חשבונית מס או חשבונית מס/קבלה בלבד.'
            )
            return room
        room.net = original.subtotal - original.discount_amount
    elif number.startswith('SD-'):
        return CreditRoom(
            number, known=True, kind='store', document_type='transaction_invoice',
            document_type_label='חשבונית עסקה',
            refusal=f'{number} הוא חשבונית עסקה של החנות — דרישת תשלום ולא מסמך מס, ואין מה לזכות בה.',
        )
    else:
        lessons = Invoice.objects.select_for_update() if lock else Invoice.objects
        sales = StoreInvoice.objects.select_for_update() if lock else StoreInvoice.objects
        lesson = lessons.filter(invoice_number=number).first()
        sale = None if lesson else sales.filter(invoice_number=number).first()
        if lesson is None and sale is None:
            return CreditRoom(number)
        room = CreditRoom(
            number, known=True, kind='lesson' if lesson else 'store', document_type='combined',
            document_type_label='חשבונית מס/קבלה',
            document_date=original_document_date(number),
        )
        if lesson is not None and lesson.status in ('pending', 'failed'):
            room.refusal = f'{number} לא הפך למסמך (החיוב לא הושלם), ואין מה לזכות בו.'
            return room
        room.net = split_vat_inclusive(lesson.amount if lesson else sale.total_amount)[0]

    room.credited = (
        FormalDocument.objects.filter(document_type='credit_invoice')
        .filter(Q(linked_document_number=number) | (Q(linked_document=original) if original else Q(pk__in=[])))
        .aggregate(total=Sum('subtotal'))['total'] or Decimal('0')
    )
    return room


def _check_creditable(number: str, data: dict, amount_before_vat: Decimal):
    """
    The original a credit note credits, checked — or None for a number kogo
    never issued (the previous software's, which only its date can vouch for).

    A FormalDocument must be a tax invoice or invoice-receipt of the same
    customer. Whatever kogo issued it through — the office, a lesson receipt
    (IR) or a store sale (ST) — the credit may not pass what is left of it
    before VAT: its amount less the credit notes already issued against it
    (credit_room, read under a lock on the original).
    """
    if not number:
        return None
    room = credit_room(number, lock=True)
    if not room.known:
        return None
    if room.refusal:
        raise ValueError(room.refusal)
    original = room.original
    if original is not None:
        other_child = original.child_id and str(original.child_id) != str(data.get('child_id') or '')
        other_business = (
            original.business_customer_id
            and str(original.business_customer_id) != str(data.get('business_customer_id') or '')
        )
        if other_child or other_business:
            raise ValueError(f'{number} הונפק ללקוח אחר. זיכוי ניתן רק ללקוח שקיבל את המסמך המקורי.')
    left = room.left
    if amount_before_vat > left:
        raise ValueError(
            f'אפשר לזכות את {number} עד {_money_text(max(left, Decimal("0")))} לפני מע"מ '
            f'(סכומו {_money_text(room.net)}, וכבר זוכו {_money_text(room.credited)}).'
        )
    return original


def original_document_date(number: str):
    """
    The date of the document `number`, from whichever of kogo's runs issued it.

    A credit note prints the original's number and date. The original may be a
    document issued by hand, a lesson receipt (IR) or a store sale (ST/SD); a
    number kogo never issued — the previous software's — gives None.
    """
    number = (number or '').strip()
    if not number:
        return None
    from apps.customers.financial_models import Invoice
    from apps.store.models import StoreInvoice

    formal = FormalDocument.objects.filter(document_number=number).values_list('document_date', flat=True).first()
    if formal:
        return formal
    for moment in (
        Invoice.objects.filter(invoice_number=number).values_list('invoice_date', flat=True).first(),
        StoreInvoice.objects.filter(invoice_number=number).values_list('issue_date', flat=True).first(),
    ):
        if moment:
            return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()
    return None


def _credit_note_recipient(doc: FormalDocument) -> tuple[str, str]:
    """Return (customer_name, email) for a credit note, or ('', '') if unreachable."""
    if doc.business_customer_id:
        customer = doc.business_customer
        return customer.full_name, (customer.email or '').strip()
    if doc.child_id:
        family = getattr(doc.child, 'family', None)
        return doc.child.full_name, (family.email or '').strip() if family else ''
    return '', ''


def _email_credit_note(doc: FormalDocument, *, customer_name: str | None = None, email: str | None = None) -> bool:
    """Send a credit note to the customer with its PDF attached. True when it was sent."""
    from apps.core.credit_note_email import CreditNote, send_credit_note_email
    from apps.documents.document_pdf import generate_document_pdf
    from apps.documents import signing

    default_name, default_email = _credit_note_recipient(doc)
    name = customer_name or default_name or (doc.customer_name or '')
    email = email or default_email
    if not email:
        logger.info('No email for credit note %s — not sent', doc.document_number)
        return False

    claim = None
    if signing.enabled():
        # The signed original, once; or no mail, with the reason on its row.
        from apps.documents.models import SignedOriginal
        from apps.documents.signing.service import KIND_FORMAL, claim_email

        claim = claim_email(KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_CREDIT_NOTE, email_to=email)
        if claim is None:
            return False

    linked = doc.linked_document
    try:
        sent = send_credit_note_email(
            CreditNote(
                customer_name=name,
                email=email,
                amount=doc.total_amount,
                reason=doc.credit_reason,
                original_number=doc.linked_document_number or (linked.document_number if linked else ''),
                original_date=doc.linked_document_date or (linked.document_date if linked else None),
                document_number=doc.document_number,
                issued_at=doc.document_date,
            ),
            pdf_bytes=claim.pdf if claim is not None else generate_document_pdf(doc),
            pdf_filename=f'{doc.document_number}.pdf',
        )
    except Exception as exc:
        if claim is not None:
            claim.failed(exc)
        raise
    if claim is not None:
        if sent:
            claim.sent()
        else:
            # No provider: nothing left the system, so the original is still unsent.
            claim.failed(RuntimeError('not sent'))
    return bool(sent)


def issue_refund_credit_note(
    *,
    gross_amount,
    reason: str,
    original_number: str,
    original_date=None,
    child=None,
    customer_name: str = '',
    email: str = '',
    branch_id=None,
    business_id=None,
) -> FormalDocument:
    """
    The הודעת זיכוי a refund owes the customer.

    A refund is corrected by a further, numbered document — never by editing the
    original (סעיף 23(ב), 23א). This one carries what סעיף 9(ה) asks for: the
    original's number and date, the reason, and the amount split into VAT. The
    amount refunded is gross, so the split comes from it, not the other way
    round — a credit of ₪49.00 must total exactly ₪49.00.
    """
    from apps.core.vat import split_vat_inclusive

    before, vat, total = split_vat_inclusive(gross_amount)
    if hasattr(original_date, 'date') and callable(original_date.date):
        original_date = timezone.localtime(original_date).date() if timezone.is_aware(original_date) else original_date.date()

    with transaction.atomic():
        doc = FormalDocument.objects.create(
            document_number=_generate_document_number('credit_invoice'),
            document_type='credit_invoice',
            client_type='existing',
            child=child,
            customer_name=(customer_name or '').strip() or None,
            branch_id=branch_id,
            business_id=business_id,
            # Dated by the system, today: not put to validate_document_date,
            # since the money has already gone back and the note must follow it.
            document_date=israel_today(),
            vat_exempt=False,
            vat_percent=Decimal('18'),
            subtotal=before,
            discount_amount=Decimal('0'),
            discount_percent=Decimal('0'),
            vat_amount=vat,
            total_amount=total,
            linked_document_number=original_number or '',
            linked_document_date=original_date,
            credit_reason=reason or 'זיכוי',
            internal_notes='הופק אוטומטית עם זיכוי העסקה',
            **_issued(None),
        )
        from apps.documents import signing

        if signing.enabled():
            from apps.documents.models import SignedOriginal

            # Addressed by the caller (a walk-in buyer has no card to look it
            # up on), so the address is kept with the original for the cron.
            _sign_at_issue(
                doc, channel=SignedOriginal.CHANNEL_CREDIT_NOTE,
                email_to=(email or '').strip(), customer_name=(customer_name or '').strip(),
            )
            doc_id = doc.pk
            transaction.on_commit(lambda: _email_credit_note_after_commit(
                doc_id, customer_name=customer_name or None, email=email or None,
            ))
            return doc

    try:
        _email_credit_note(doc, customer_name=customer_name or None, email=email or None)
    except Exception:
        logger.exception('Credit note email failed for %s (non-fatal)', doc.document_number)
    return doc


def _receipt_amount(receipt: dict) -> Decimal:
    method = receipt.get('payment_method', 'מזומן')
    if method == 'מזומן':
        return Decimal(str(receipt.get('cash_amount', 0)))
    if method == "צ'ק":
        confirmed = [c for c in receipt.get('checks', []) if c.get('confirmed') and c.get('amount', 0) > 0]
        return sum(Decimal(str(c['amount'])) for c in confirmed)
    if method == 'אשראי':
        return Decimal(str(receipt.get('card_amount', 0)))
    if method == 'העברה בנקאית':
        return Decimal(str(receipt.get('bank_amount', 0)))
    return Decimal('0')


def _map_payment_method(hebrew: str) -> str:
    return {
        'מזומן': 'cash',
        "צ'ק": 'check',
        'אשראי': 'credit_card',
        'העברה בנקאית': 'bank_transfer',
    }.get(hebrew, 'cash')


_PAYMENT_KEYS = ('cash', 'check', 'credit_card', 'bank_transfer')


def _payment_key(method: str) -> str:
    """A payment method as stored: the dialog's Hebrew label or the stored key itself."""
    return method if method in _PAYMENT_KEYS else _map_payment_method(method)


def _withholding(value) -> Decimal | None:
    """ניכוי במקור as stored: None when none was withheld."""
    amount = Decimal(str(value or 0))
    if amount < 0:
        raise ValueError('ניכוי במקור אינו יכול להיות שלילי')
    return amount if amount > 0 else None


def _money_text(amount: Decimal) -> str:
    return f'{amount:,.2f} ₪'


def _combined_payment_rows(data: dict, invoice_data: dict, total: Decimal) -> list[dict]:
    """
    The payment rows of a חשבונית מס/קבלה (G) — how much was paid each way.

    Each way it was paid is a row of its own amount, written as a receipt's
    are, with what identifies it: a check's number, bank, branch, account and
    due date (הוראה 5(ב)); a card's last four digits, brand and installments;
    a transfer's reference and value date. Together with any ניכוי במקור they
    come to the document's total exactly — a receipt for more or less than the
    invoice is not what was paid.

    Every method used to be written for the whole total, so a document paid
    half in cash and half by check reported twice its money in the uniform
    file. The older payload (payment_methods, names only) is still read when it
    names one method — that method paid it all; naming several is refused,
    since it cannot say how much each paid.
    """
    rows = invoice_data.get('payments') or []
    withheld = _withholding(invoice_data.get('withholding_amount')) or Decimal('0')
    due = total - withheld
    # The older payload's single flag: the check it was paid with is crossed
    # "לא סחיר" in the customer's name (הוראה 18ב(ד)(2)).
    legacy_crossed = data.get('check_crossed') is True or invoice_data.get('check_crossed') is True

    if not rows:
        methods = invoice_data.get('payment_methods') or []
        if not methods:
            return []
        if len(methods) > 1:
            raise ValueError('חשבונית מס/קבלה בכמה אמצעי תשלום צריכה את הסכום של כל אחד מהם')
        method = _payment_key(methods[0])
        return [{
            'payment_method': method,
            'amount': due,
            'check_crossed': legacy_crossed if method == 'check' else False,
        }]

    out = []
    for row in rows:
        method = _payment_key(row['method'])
        amount = Decimal(str(row['amount']))
        if amount <= 0:
            raise ValueError('סכום של אמצעי תשלום חייב להיות גדול מאפס')
        payment = {'payment_method': method, 'amount': amount, 'notes': row.get('notes', '') or ''}
        if method == 'check':
            payment.update(
                reference=row.get('check_number', '') or '',
                check_date=row.get('check_date') or None,
                check_bank=row.get('check_bank', '') or '',
                check_branch=row.get('check_branch', '') or '',
                check_account=row.get('check_account', '') or '',
                check_crossed=row.get('check_crossed') is True or legacy_crossed,
            )
        elif method == 'credit_card':
            payment.update(
                card_last_four=row.get('card_last_four', '') or '',
                card_brand=(row.get('card_brand') or '').strip() or None,
                card_installments=row.get('installments') or 1,
                reference=row.get('reference', '') or '',
                paid_on=row.get('paid_on') or None,
            )
        else:
            payment.update(reference=row.get('reference', '') or '', paid_on=row.get('paid_on') or None)
        out.append(payment)

    paid = sum((row['amount'] for row in out), Decimal('0'))
    if paid != due:
        withheld_note = f' פחות ניכוי במקור של {_money_text(withheld)}' if withheld else ''
        raise ValueError(
            f'סכומי אמצעי התשלום ({_money_text(paid)}) אינם שווים לסכום החשבונית '
            f'({_money_text(total)}{withheld_note}). כל שקל שהתקבל נרשם פעם אחת, באמצעי שבו שולם.'
        )
    return out


def _attempt_tranzila(doc: FormalDocument) -> None:
    """Try to issue a formal document via Tranzila. Fail silently — local record always saved."""
    from django.conf import settings
    billing_terminal = getattr(settings, 'TRANZILA_BILLING_TERMINAL', '')
    if not billing_terminal:
        logger.info(f"TRANZILA_BILLING_TERMINAL not configured — skipping Tranzila issuance for {doc.document_number}")
        return

    tranzila_type = TRANZILA_DOCUMENT_TYPE.get(doc.document_type)
    if not tranzila_type:
        return

    try:
        from apps.core.tranzila_service import TranzilaService
        svc = TranzilaService()

        from apps.documents import signing

        client_name = ''
        client_email = ''
        if doc.child_id:
            client_name = doc.child.full_name
            family = getattr(doc.child, 'family', None)
            # With an email Tranzila may mail its own copy of the document —
            # unsigned, and a second original. Once kogo signs and mails its
            # originals itself, Tranzila is not given the address.
            if family and not signing.enabled():
                client_email = (family.email or '').strip()

        result = svc.create_formal_document(
            terminal_name=billing_terminal,
            document_type=tranzila_type,
            document_date=str(doc.document_date),
            items=[
                {
                    'name': item.description or item.sku or 'פריט',
                    'units_number': float(item.quantity),
                    'unit_price': float(item.unit_price),
                }
                for item in doc.line_items.all()
            ] or [{'name': doc.description or 'שירות', 'units_number': 1, 'unit_price': float(doc.total_amount)}],
            payments=[
                {'payment_method': p.payment_method, 'amount': float(p.amount)}
                for p in doc.payments.all()
            ],
            vat_percent=float(doc.vat_percent) if not doc.vat_exempt else 0,
            client_name=client_name,
            client_email=client_email,
            prices_include_vat=doc.prices_include_vat,
        )

        parsed = svc.parse_billing_document_response(result)
        if parsed.get('success'):
            doc.tranzila_doc_id = parsed.get('doc_id', '')
            doc.tranzila_retrieval_key = parsed.get('retrieval_key', '')
            doc.pdf_url = parsed.get('pdf_url', '')
            doc.tranzila_issued = True
            doc.save(update_fields=['tranzila_doc_id', 'tranzila_retrieval_key', 'pdf_url', 'tranzila_issued'])
            logger.info(f"Tranzila document issued: {doc.tranzila_doc_id} for {doc.document_number}")
        else:
            logger.warning(f"Tranzila document issuance failed for {doc.document_number}: {parsed.get('error') or result}")

    except Exception as e:
        logger.error(f"Tranzila document issuance exception for {doc.document_number}: {e}", exc_info=True)
