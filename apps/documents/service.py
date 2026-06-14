import logging
from decimal import Decimal
from django.utils import timezone
from django.db import transaction

from apps.documents.models import (
    DocumentCounter, FormalDocument, DocumentLineItem, DocumentPayment,
    DOCUMENT_NUMBER_PREFIX, TRANZILA_DOCUMENT_TYPE,
)

logger = logging.getLogger(__name__)

VAT_RATE = Decimal('0.18')


def _generate_document_number(document_type: str) -> str:
    year = timezone.now().year
    prefix = DOCUMENT_NUMBER_PREFIX.get(document_type, 'DOC')
    seq = DocumentCounter.next_number(document_type, year)
    return f"{prefix}-{year}-{seq:04d}"


def _compute_totals(line_items: list, discount_amount: Decimal, discount_percent: Decimal,
                    vat_exempt: bool, round_total: bool) -> dict:
    subtotal = sum(Decimal(str(i['quantity'])) * Decimal(str(i['price'])) for i in line_items)
    effective_discount = discount_amount if discount_amount > 0 else (subtotal * discount_percent / 100)
    base = subtotal - effective_discount
    vat = Decimal('0') if vat_exempt else (base * VAT_RATE).quantize(Decimal('0.01'))
    total = base + vat
    if round_total:
        total = total.quantize(Decimal('1'))
    return {
        'subtotal': subtotal,
        'discount_amount': effective_discount,
        'vat_amount': vat,
        'total_amount': total,
    }


@transaction.atomic
def create_invoice(data: dict, document_type: str) -> FormalDocument:
    """Create a tax invoice or transaction invoice."""
    invoice_data = data['invoice_details']
    totals = _compute_totals(
        invoice_data['line_items'],
        Decimal(str(invoice_data.get('discount_amount', 0))),
        Decimal(str(invoice_data.get('discount_percent', 0))),
        invoice_data.get('vat_exempt', False),
        invoice_data.get('round_total', False),
    )

    doc = FormalDocument.objects.create(
        document_number=_generate_document_number(document_type),
        document_type=document_type,
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        document_date=invoice_data['document_date'],
        due_date=invoice_data.get('due_date') or None,
        description=invoice_data.get('description', ''),
        currency=invoice_data.get('currency', 'ILS'),
        prices_include_vat=invoice_data.get('prices_include_vat', False),
        payment_terms=invoice_data.get('payment_terms', ''),
        vat_exempt=invoice_data.get('vat_exempt', False),
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

    _attempt_tranzila(doc)
    return doc


@transaction.atomic
def create_combined(data: dict) -> FormalDocument:
    """Create a combined tax invoice + receipt."""
    invoice_data = data['invoice_details']
    totals = _compute_totals(
        invoice_data['line_items'],
        Decimal(str(invoice_data.get('discount_amount', 0))),
        Decimal(str(invoice_data.get('discount_percent', 0))),
        invoice_data.get('vat_exempt', False),
        invoice_data.get('round_total', False),
    )

    doc = FormalDocument.objects.create(
        document_number=_generate_document_number('combined'),
        document_type='combined',
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        document_date=invoice_data['document_date'],
        due_date=invoice_data.get('due_date') or None,
        description=invoice_data.get('description', ''),
        currency=invoice_data.get('currency', 'ILS'),
        prices_include_vat=invoice_data.get('prices_include_vat', False),
        payment_terms=invoice_data.get('payment_terms', ''),
        vat_exempt=invoice_data.get('vat_exempt', False),
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

    for pm in invoice_data.get('payment_methods', []):
        DocumentPayment.objects.create(
            document=doc,
            payment_method=_map_payment_method(pm),
            amount=doc.total_amount,
        )

    _attempt_tranzila(doc)
    return doc


@transaction.atomic
def create_receipt(data: dict) -> FormalDocument:
    """Create a standalone receipt."""
    receipt = data['receipt_details']
    method_key = _map_payment_method(receipt['payment_method'])
    amount = _receipt_amount(receipt)

    doc = FormalDocument.objects.create(
        document_number=_generate_document_number('receipt'),
        document_type='receipt',
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        document_date=data.get('document_date', str(timezone.now().date())),
        currency='ILS',
        vat_exempt=True,
        vat_percent=Decimal('18'),
        subtotal=amount,
        discount_amount=Decimal('0'),
        discount_percent=Decimal('0'),
        vat_amount=Decimal('0'),
        total_amount=amount,
        linked_document_number=receipt.get('linked_invoice_id', ''),
        customer_notes=receipt.get('check_notes', '') or receipt.get('cash_notes', '') or receipt.get('bank_notes', '') or receipt.get('card_notes', ''),
    )

    payment_kwargs = dict(
        document=doc,
        payment_method=method_key,
        amount=amount,
    )
    if method_key == 'check':
        confirmed = [c for c in receipt.get('checks', []) if c.get('confirmed') and c.get('amount', 0) > 0]
        for chk in confirmed:
            DocumentPayment.objects.create(
                document=doc,
                payment_method='check',
                amount=Decimal(str(chk['amount'])),
                reference=chk.get('check_number', ''),
                check_date=chk.get('date') or None,
                check_bank=chk.get('bank', ''),
                check_branch=chk.get('branch', ''),
                check_account=chk.get('account_number', ''),
            )
    elif method_key == 'credit_card':
        DocumentPayment.objects.create(
            document=doc,
            payment_method='credit_card',
            amount=Decimal(str(receipt.get('card_amount', 0))),
            card_last_four=receipt.get('card_last_four', ''),
            card_expiry=receipt.get('card_expiry', ''),
            card_installments=receipt.get('card_installments', 1),
            notes=receipt.get('card_notes', ''),
        )
    elif method_key == 'bank_transfer':
        DocumentPayment.objects.create(
            document=doc,
            payment_method='bank_transfer',
            amount=Decimal(str(receipt.get('bank_amount', 0))),
            reference=receipt.get('bank_reference', ''),
            notes=receipt.get('bank_notes', ''),
        )
    else:
        DocumentPayment.objects.create(**payment_kwargs, notes=receipt.get('cash_notes', ''))

    _attempt_tranzila(doc)
    return doc


@transaction.atomic
def create_credit_invoice(data: dict) -> FormalDocument:
    """Create a credit note (חשבונית מס זיכוי)."""
    credit = data['credit_invoice_details']
    amount_before_vat = Decimal(str(credit['credit_amount_before_vat']))
    vat_exempt = credit.get('vat_exempt', False)
    vat_amount = Decimal('0') if vat_exempt else (amount_before_vat * VAT_RATE).quantize(Decimal('0.01'))
    total = amount_before_vat + vat_amount

    # Try to resolve linked document
    linked_number = credit.get('linked_invoice_id', '').strip()
    linked_doc = None
    if linked_number:
        try:
            linked_doc = FormalDocument.objects.get(document_number=linked_number)
        except FormalDocument.DoesNotExist:
            pass

    doc = FormalDocument.objects.create(
        document_number=_generate_document_number('credit_invoice'),
        document_type='credit_invoice',
        client_type=data['client_type'],
        child_id=data.get('child_id'),
        business_customer_id=data.get('business_customer_id'),
        document_date=credit['document_date'],
        vat_exempt=vat_exempt,
        vat_percent=Decimal('18'),
        subtotal=amount_before_vat,
        discount_amount=Decimal('0'),
        discount_percent=Decimal('0'),
        vat_amount=vat_amount,
        total_amount=total,
        linked_document=linked_doc,
        linked_document_number=linked_number,
        credit_reason=credit.get('credit_reason', ''),
        customer_notes=credit.get('customer_notes', ''),
        internal_notes=credit.get('internal_notes', ''),
    )

    _attempt_tranzila(doc)
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
        result = svc.create_formal_document(
            terminal_name=billing_terminal,
            document_type=tranzila_type,
            document_date=str(doc.document_date),
            items=[
                {
                    'description': item.description or item.sku or 'פריט',
                    'quantity': float(item.quantity),
                    'unit_price': float(item.unit_price),
                }
                for item in doc.line_items.all()
            ] or [{'description': doc.description or 'שירות', 'quantity': 1, 'unit_price': float(doc.total_amount)}],
            payments=[
                {'payment_type': p.payment_method, 'amount': float(p.amount)}
                for p in doc.payments.all()
            ],
            vat_percent=float(doc.vat_percent) if not doc.vat_exempt else 0,
        )

        if result.get('success') or result.get('doc_id'):
            doc.tranzila_doc_id = str(result.get('doc_id', ''))
            doc.tranzila_retrieval_key = str(result.get('retrieval_key', ''))
            doc.pdf_url = result.get('pdf_url', '')
            doc.tranzila_issued = True
            doc.save(update_fields=['tranzila_doc_id', 'tranzila_retrieval_key', 'pdf_url', 'tranzila_issued'])
            logger.info(f"Tranzila document issued: {doc.tranzila_doc_id} for {doc.document_number}")
        else:
            logger.warning(f"Tranzila document issuance failed for {doc.document_number}: {result}")

    except Exception as e:
        logger.error(f"Tranzila document issuance exception for {doc.document_number}: {e}", exc_info=True)
