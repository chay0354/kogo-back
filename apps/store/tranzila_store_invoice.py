"""Issue official Tranzila tax documents for store (website) purchases."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.tranzila_service import TranzilaService
from apps.documents.models import DocumentLineItem, FormalDocument
from apps.documents.service import _generate_document_number
from apps.store.models import StoreInvoice

logger = logging.getLogger(__name__)

STORE_PAYMENT_METHOD = {
    'credit_card': 1,
    'cash': 5,
    'monthly_billing': 1,
}


def _billing_terminal() -> str:
    """Billing terminal for tax documents; falls back to payment terminal."""
    explicit = (getattr(settings, 'TRANZILA_BILLING_TERMINAL', '') or '').strip()
    if explicit:
        return explicit
    return (getattr(settings, 'TRANZILA_TERMINAL', '') or '').strip()


def _customer_details(invoice: StoreInvoice) -> tuple[str, str, str]:
    name = (invoice.customer_name or '').strip()
    email = (invoice.customer_email or '').strip()
    phone = (invoice.customer_phone or '').strip()
    if invoice.child_id:
        if not name:
            name = invoice.child.full_name
        family = getattr(invoice.child, 'family', None)
        if family and not email:
            email = (family.email or '').strip()
        if family and not phone:
            phone = (family.phone or '').strip()
    return name or 'לקוח', email, phone


def _build_tranzila_items(invoice: StoreInvoice) -> list[dict]:
    items = []
    for sale in invoice.line_items.select_related('product').all():
        name = sale.product.name if sale.product_id else 'פריט'
        if sale.size:
            name = f'{name} ({sale.size})'
        items.append({
            'name': name,
            'unit_price': float(sale.unit_price),
            'units_number': float(sale.quantity),
        })
    return items


@transaction.atomic
def issue_store_tranzila_document(invoice: StoreInvoice) -> FormalDocument | None:
    """
    Issue a Tranzila combined tax invoice/receipt (IR) for a paid store invoice.
    Idempotent — returns the linked FormalDocument if already issued.
    """
    invoice = (
        StoreInvoice.objects
        .select_related('child', 'child__family', 'formal_document', 'branch')
        .prefetch_related('line_items__product')
        .get(pk=invoice.pk)
    )

    if invoice.formal_document_id and invoice.formal_document.tranzila_issued:
        return invoice.formal_document

    terminal = _billing_terminal()
    if not terminal or terminal == 'mock-terminal':
        logger.info(
            'No Tranzila billing terminal configured — skipping Tranzila doc for %s',
            invoice.invoice_number,
        )
        return None

    items = _build_tranzila_items(invoice)
    if not items:
        logger.warning('No line items for Tranzila document on %s', invoice.invoice_number)
        return None

    customer_name, customer_email, customer_phone = _customer_details(invoice)
    payment_method = STORE_PAYMENT_METHOD.get(invoice.payment_method, 1)
    document_date = str(timezone.localtime(invoice.issue_date).date())

    svc = TranzilaService()
    result = svc.create_formal_document(
        terminal_name=terminal,
        document_type='IR',
        document_date=document_date,
        items=items,
        payments=[{'payment_method': payment_method, 'amount': float(invoice.total_amount)}],
        vat_percent=18.0,
        client_name=customer_name,
        client_email=customer_email,
        client_phone=customer_phone,
        prices_include_vat=True,
    )
    parsed = svc.parse_billing_document_response(result)
    if not parsed.get('success'):
        logger.warning(
            'Tranzila document failed for store invoice %s: %s',
            invoice.invoice_number,
            parsed.get('error') or result,
        )
        return None

    doc_number = parsed.get('document_number') or _generate_document_number('combined')
    formal = invoice.formal_document
    if formal is None:
        formal = FormalDocument(
            document_number=doc_number,
            document_type='combined',
            client_type='existing',
            child_id=invoice.child_id,
            branch_id=invoice.branch_id,
            document_date=timezone.localtime(invoice.issue_date).date(),
            description=f'רכישה בחנות — {invoice.website_order_number or invoice.invoice_number}',
            currency='ILS',
            prices_include_vat=True,
            vat_percent=Decimal('18'),
            subtotal=invoice.total_amount,
            discount_amount=Decimal('0'),
            discount_percent=Decimal('0'),
            vat_amount=Decimal('0'),
            total_amount=invoice.total_amount,
            customer_notes=invoice.website_order_number or '',
        )
    formal.tranzila_doc_id = parsed.get('doc_id', '')
    formal.tranzila_retrieval_key = parsed.get('retrieval_key', '')
    formal.pdf_url = parsed.get('pdf_url', '')
    formal.tranzila_issued = True
    formal.save()

    if not formal.line_items.exists():
        for sale in invoice.line_items.select_related('product').all():
            name = sale.product.name if sale.product_id else 'פריט'
            if sale.size:
                name = f'{name} ({sale.size})'
            DocumentLineItem.objects.create(
                document=formal,
                description=name,
                quantity=Decimal(str(sale.quantity)),
                unit_price=sale.unit_price,
            )

    StoreInvoice.objects.filter(pk=invoice.pk).update(formal_document_id=formal.id)
    logger.info(
        'Tranzila store document %s linked to invoice %s',
        formal.tranzila_doc_id,
        invoice.invoice_number,
    )
    return formal


def get_store_tranzila_pdf_bytes(invoice: StoreInvoice) -> bytes | None:
    """Fetch official Tranzila PDF bytes for a store invoice, if issued."""
    invoice = StoreInvoice.objects.select_related('formal_document').get(pk=invoice.pk)
    formal = invoice.formal_document
    if not formal or not formal.tranzila_issued or not formal.tranzila_doc_id:
        return None

    terminal = _billing_terminal()
    if not terminal or terminal == 'mock-terminal':
        return None

    return TranzilaService().get_formal_document_pdf(terminal, formal.tranzila_doc_id)


def tranzila_pdf_public_url(invoice: StoreInvoice) -> str:
    formal = getattr(invoice, 'formal_document', None)
    if formal and formal.tranzila_retrieval_key:
        return f'https://my.tranzila.com/api/get_financial_document/{formal.tranzila_retrieval_key}'
    return ''
