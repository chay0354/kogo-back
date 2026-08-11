"""Issue Tranzila formal documents for completed website store purchases."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.core.tranzila_service import TranzilaService
from apps.store.models import StoreInvoice

logger = logging.getLogger(__name__)


def issue_store_tranzila_document(
    invoice: StoreInvoice,
    *,
    card_last4: str = '',
) -> bool:
    """
    Create a Tranzila IR (invoice + receipt) for a paid store invoice.
    Idempotent — skips if already issued or billing terminal is not configured.
    """
    if invoice.tranzila_issued:
        return True

    billing_terminal = (getattr(settings, 'TRANZILA_BILLING_TERMINAL', '') or '').strip()
    if not billing_terminal:
        logger.info(
            'TRANZILA_BILLING_TERMINAL not configured — skipping Tranzila doc for %s',
            invoice.invoice_number,
        )
        return False

    invoice = (
        StoreInvoice.objects
        .select_related('child')
        .prefetch_related('line_items__product')
        .get(pk=invoice.pk)
    )

    items = []
    for sale in invoice.line_items.all():
        name = sale.product.name if sale.product_id else 'פריט'
        if sale.size:
            name = f'{name} ({sale.size})'
        items.append({
            'name': name,
            'unit_price': float(sale.unit_price),
            'units_number': float(sale.quantity),
            'price_type': 'G',
            'type': 'I',
        })
    if not items:
        items.append({
            'name': 'רכישה בחנות קוגומלו',
            'unit_price': float(invoice.total_amount),
            'units_number': 1.0,
            'price_type': 'G',
            'type': 'I',
        })

    payment = {
        'payment_method': 1,
        'amount': float(invoice.total_amount),
        'payment_date': timezone.localtime(invoice.issue_date).strftime('%Y-%m-%d'),
    }
    if card_last4:
        payment['cc_last_4_digits'] = str(card_last4)[-4:]
    txn = (invoice.tranzila_transaction_id or '').strip()
    if txn.isdigit():
        payment['txnindex'] = int(txn)

    customer_name = (invoice.customer_name or '').strip()
    if invoice.child_id and invoice.child:
        customer_name = (invoice.child.full_name or customer_name).strip()

    try:
        result = TranzilaService().create_formal_document(
            terminal_name=billing_terminal,
            document_type='IR',
            document_date=timezone.localtime(invoice.issue_date).strftime('%Y-%m-%d'),
            items=items,
            payments=[payment],
            vat_percent=18.0,
            client_name=customer_name,
            client_email=(invoice.customer_email or '').strip(),
        )
    except Exception:
        logger.exception('Tranzila document request failed for %s', invoice.invoice_number)
        return False

    if not result.get('success'):
        logger.warning(
            'Tranzila document issuance failed for %s: %s',
            invoice.invoice_number,
            result.get('error'),
        )
        return False

    StoreInvoice.objects.filter(pk=invoice.pk).update(
        tranzila_doc_id=result.get('doc_id', ''),
        tranzila_retrieval_key=result.get('retrieval_key', ''),
        tranzila_document_number=result.get('document_number', ''),
        pdf_url=result.get('pdf_url', ''),
        tranzila_issued=True,
    )
    logger.info(
        'Tranzila document issued for %s → doc %s pdf %s',
        invoice.invoice_number,
        result.get('doc_id'),
        result.get('pdf_url'),
    )
    return True
