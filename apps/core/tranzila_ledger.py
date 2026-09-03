"""
Unified Tranzila documents + transactions for the CRM invoices page.

מסמכים = tax documents issued for Tranzila charges (live billing API, plus local
FormalDocument / CRM / store invoices that belong to those charges).
תשלומים = widget (CRM Payment) and store charges only — not every
transaction on the Tranzila terminal, and not ₪0 card-verify signups.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.tranzila_service import TranzilaService, is_tranzila_approved

TRANZILA_PDF_PUBLIC_BASE = TranzilaService.TRANZILA_PDF_PUBLIC_BASE

logger = logging.getLogger(__name__)


def _tranzila_client() -> TranzilaService:
    """Prefer the production terminal; fall back to the default terminal if prod keys are empty."""
    production = TranzilaService.production()
    if production.credential_error() is None:
        return production
    default = TranzilaService()
    if default.credential_error() is None:
        return default
    return production

TRANZILA_DOC_TYPE_LABELS = {
    'IR': 'חשבונית מס/קבלה',
    'IN': 'חשבונית מס',
    'RE': 'קבלה',
    'DI': 'חשבונית עסקה',
    'CN': 'חשבונית מס זיכוי',
}

LOCAL_DOC_TYPE_LABELS = {
    'tax_invoice': 'חשבונית מס',
    'receipt': 'קבלה',
    'combined': 'חשבונית מס/קבלה',
    'transaction_invoice': 'חשבונית עסקה',
    'credit_invoice': 'חשבונית מס זיכוי',
    'draft': 'טיוטה',
}

PAYMENT_METHOD_LABELS = {
    'credit_card': 'אשראי',
    'cash': 'מזומן',
    'bank_transfer': 'העברה בנקאית',
    'check': "צ'ק",
    'monthly_billing': 'הוראת קבע',
}


def _parse_amount(raw) -> float:
    if raw in (None, ''):
        return 0.0
    try:
        return float(Decimal(str(raw).replace(',', '').strip()))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


def _iso_date(value) -> str:
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        text = value.isoformat()
        return text[:10] if 'T' in text or len(text) >= 10 else text
    text = str(value).strip()
    if not text:
        return ''
    return text.replace(' ', 'T')[:10]


def _iso_datetime(value) -> str:
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    text = str(value).strip().replace(' ', 'T')
    parsed = parse_datetime(text)
    return parsed.isoformat() if parsed else text


def _default_range(start: Optional[date], end: Optional[date]) -> tuple[date, date]:
    today = timezone.localdate()
    return (start or (today - timedelta(days=90)), end or today)


def _document_status_from_tranzila(doc_type: str, action) -> str:
    code = (doc_type or '').upper()
    if code in {'CN', 'CR'} or str(action) in {'2', '3'}:
        return 'refunded'
    if code in {'IR', 'RE'}:
        return 'completed'
    return 'pending'


def normalize_tranzila_document(row: dict, customer_name: str = '') -> dict:
    doc_id = str(row.get('id') or row.get('document_id') or '')
    number = str(row.get('number') or row.get('document_number') or '')
    doc_type = str(row.get('type') or row.get('document_type') or '').upper()
    retrieval_key = str(row.get('retrieval_key') or '')
    pdf_url = str(row.get('pdf_url') or '')
    if not pdf_url and retrieval_key:
        pdf_url = f'{TRANZILA_PDF_PUBLIC_BASE}/{retrieval_key}'
    created = row.get('created_at') or row.get('document_date') or ''
    status = _document_status_from_tranzila(doc_type, row.get('action'))
    amount = _parse_amount(row.get('total_charge_amount') or row.get('amount') or row.get('total'))
    paid = amount if status == 'completed' else 0.0
    return {
        'id': f'tranzila-doc-{doc_id or number}',
        'document_number': number or doc_id,
        'issue_date': _iso_date(created),
        'customer_name': customer_name or str(row.get('client_name') or row.get('contact') or ''),
        'document_type': TRANZILA_DOC_TYPE_LABELS.get(doc_type, doc_type or 'מסמך טרנזילה'),
        'document_type_code': doc_type,
        'total_amount': amount,
        'amount_paid': paid,
        'open_balance': 0.0 if status == 'completed' else amount,
        'status': status,
        'pdf_url': pdf_url,
        'tranzila_doc_id': doc_id,
        'source': 'tranzila',
        'branch': '',
        'branch_id': None,
    }


def normalize_tranzila_transaction(row: dict) -> dict:
    index = str(
        row.get('index')
        or row.get('transaction_index')
        or row.get('transaction_id')
        or ''
    )
    txn_date = row.get('transaction_date') or row.get('created_at') or ''
    txn_time = row.get('transaction_time') or ''
    created = f'{txn_date}T{txn_time}'.strip('T') if txn_date else ''
    response_code = row.get('processor_response_code') or row.get('response_code') or row.get('Response')
    approved = is_tranzila_approved(response_code)
    amount = _parse_amount(row.get('amount') or row.get('sum'))
    last4 = str(row.get('credit_card_token') or row.get('ccno') or '')[-4:]
    method = row.get('txn_payment_method') or row.get('payment_method') or 'credit_card'
    method_label = PAYMENT_METHOD_LABELS.get(str(method).lower(), str(method) or 'אשראי')
    customer = (
        row.get('contact')
        or row.get('client_name')
        or row.get('user_defined_1')
        or ''
    )
    confirmation = str(row.get('authorization_number') or row.get('ConfirmationCode') or '')
    invoice_number = str(row.get('txnfdnumber') or row.get('document_number') or '')
    return {
        'id': f'tranzila-txn-{index or created}',
        'created_at': _iso_datetime(created) or _iso_datetime(txn_date),
        'customer_name': str(customer),
        'invoice_number': invoice_number or index,
        'amount': amount,
        'payment_method': method_label if method_label != 'credit_card' else 'אשראי',
        'transaction_reference': confirmation or index,
        'status': 'completed' if approved else 'failed',
        'card_last4': last4,
        'source': 'tranzila',
    }


def _local_formal_rows(start: date, end: date) -> list[dict]:
    from apps.documents.models import FormalDocument

    docs = (
        FormalDocument.objects
        .select_related('child', 'business_customer', 'branch')
        .filter(document_date__gte=start, document_date__lte=end)
        .order_by('-document_date', '-created_at')
    )
    rows = []
    for doc in docs:
        if doc.child_id:
            customer = doc.child.full_name
        elif doc.business_customer_id:
            customer = doc.business_customer.full_name
        else:
            customer = ''
        amount = _parse_amount(doc.total_amount)
        is_credit = doc.document_type == 'credit_invoice'
        is_receipt = doc.document_type in ('receipt', 'combined')
        is_draft = doc.document_type == 'draft'
        status = 'draft' if is_draft else (
            'refunded' if is_credit else ('completed' if is_receipt or doc.tranzila_issued else 'pending')
        )
        paid = amount if status == 'completed' else 0.0
        rows.append({
            'id': str(doc.id),
            'document_number': doc.document_number,
            'issue_date': _iso_date(doc.document_date),
            'customer_name': customer,
            'document_type': LOCAL_DOC_TYPE_LABELS.get(doc.document_type, doc.get_document_type_display()),
            'document_type_code': doc.document_type,
            'total_amount': amount,
            'amount_paid': paid,
            'open_balance': 0.0 if status in ('completed', 'draft') else amount,
            'status': status,
            'pdf_url': doc.pdf_url or (
                f'{TRANZILA_PDF_PUBLIC_BASE}/{doc.tranzila_retrieval_key}'
                if doc.tranzila_retrieval_key else ''
            ),
            # מועד התשלום שסוכם (שוטף+30 וכדומה) — לפיו נמדד האיחור בדף הגבייה
            'due_date': _iso_date(doc.due_date) if doc.due_date else '',
            'payment_terms': doc.payment_terms or '',
            'tranzila_doc_id': doc.tranzila_doc_id,
            'source': 'tranzila' if doc.tranzila_issued else 'local',
            'tranzila_issued': doc.tranzila_issued,
            'is_draft': doc.document_type == 'draft',
            'branch': doc.branch.name if doc.branch_id else '',
            'branch_id': str(doc.branch_id) if doc.branch_id else None,
        })
    return rows


def _local_crm_invoice_rows(start: date, end: date) -> list[dict]:
    from apps.customers.financial_models import Invoice

    invoices = (
        Invoice.objects
        .select_related('family', 'payment', 'payment__tranzila_transaction', 'branch')
        .filter(invoice_date__date__gte=start, invoice_date__date__lte=end)
        .filter(
            Q(tranzila_transaction_id__gt='')
            | Q(payment__tranzila_transaction__isnull=False)
            | Q(payment_method='credit_card')
        )
        .order_by('-invoice_date')
    )
    rows = []
    for inv in invoices:
        amount = _parse_amount(inv.amount)
        status_map = {
            'paid': 'completed',
            'pending': 'pending',
            'failed': 'failed',
            'cancelled': 'failed',
            'credit': 'refunded',
        }
        status = status_map.get(inv.status, inv.status or 'pending')
        paid = amount if status == 'completed' else _parse_amount(getattr(inv, 'amount_paid', 0) or 0)
        rows.append({
            'id': f'crm-inv-{inv.id}',
            'document_number': inv.invoice_number,
            'issue_date': _iso_date(inv.invoice_date),
            'customer_name': inv.payer_name or (inv.family.name if inv.family_id else ''),
            'document_type': 'חשבונית מס/קבלה',
            'document_type_code': 'IR',
            'total_amount': amount,
            'amount_paid': paid if status == 'completed' else 0.0,
            'open_balance': 0.0 if status == 'completed' else amount,
            'status': status,
            'pdf_url': inv.pdf_url or '',
            'tranzila_doc_id': inv.tranzila_transaction_id,
            'source': 'crm',
            'branch': inv.branch.name if inv.branch_id else '',
            'branch_id': str(inv.branch_id) if inv.branch_id else None,
        })
    return rows


def _local_store_invoice_rows(start: date, end: date) -> list[dict]:
    from apps.store.models import StoreInvoice

    invoices = (
        StoreInvoice.objects
        .select_related('child', 'branch', 'formal_document')
        .filter(issue_date__date__gte=start, issue_date__date__lte=end)
        .filter(
            Q(tranzila_transaction_id__gt='')
            | Q(tranzila_txn__isnull=False)
            | Q(formal_document__isnull=False)
            | Q(payment_method='credit_card')
        )
        .order_by('-issue_date')
    )
    rows = []
    for inv in invoices:
        amount = _parse_amount(inv.total_amount)
        paid = _parse_amount(inv.amount_paid)
        status = inv.payment_status or 'pending'
        if status == 'refund_failed':
            status = 'failed'
        customer = inv.child_name if hasattr(inv, 'child_name') else ''
        if not customer:
            customer = inv.child.full_name if inv.child_id else (inv.customer_name or '')
        pdf_url = ''
        formal = getattr(inv, 'formal_document', None)
        if formal and formal.pdf_url:
            pdf_url = formal.pdf_url
        elif formal and formal.tranzila_retrieval_key:
            pdf_url = f'{TRANZILA_PDF_PUBLIC_BASE}/{formal.tranzila_retrieval_key}'
        rows.append({
            'id': str(inv.id),
            'document_number': inv.invoice_number,
            'issue_date': _iso_date(inv.issue_date),
            'customer_name': customer,
            'document_type': 'חשבונית מס/קבלה' if inv.payment_method == 'credit_card' else 'חשבונית עסקה',
            'document_type_code': 'IR' if inv.payment_method == 'credit_card' else 'DI',
            'total_amount': amount,
            'amount_paid': paid,
            'open_balance': max(amount - paid, 0.0),
            'status': status,
            'pdf_url': pdf_url,
            'store_invoice_id': str(inv.id),
            'tranzila_doc_id': (formal.tranzila_doc_id if formal else '') or inv.tranzila_transaction_id,
            'source': 'store',
            'branch': inv.branch.name if inv.branch_id else '',
            'branch_id': str(inv.branch_id) if inv.branch_id else None,
        })
    return rows


def _merge_documents(*groups: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for group in groups:
        for row in group:
            keys = []
            if row.get('tranzila_doc_id'):
                keys.append(f"doc|{row['tranzila_doc_id']}")
            if row.get('document_number'):
                keys.append(f"num|{row['document_number']}")
            if any(key in seen for key in keys):
                # Keep the Tranzila row when a local copy exists; fill blank customer.
                existing = next(
                    (
                        item for item in merged
                        if (item.get('tranzila_doc_id') and item.get('tranzila_doc_id') == row.get('tranzila_doc_id'))
                        or (item.get('document_number') and item.get('document_number') == row.get('document_number'))
                    ),
                    None,
                )
                if existing:
                    if not existing.get('customer_name') and row.get('customer_name'):
                        existing['customer_name'] = row['customer_name']
                    if not existing.get('pdf_url') and row.get('pdf_url'):
                        existing['pdf_url'] = row['pdf_url']
                    if not existing.get('store_invoice_id') and row.get('store_invoice_id'):
                        existing['store_invoice_id'] = row['store_invoice_id']
                continue
            for key in keys:
                if key:
                    seen.add(key)
            merged.append(row)
    merged.sort(key=lambda row: row.get('issue_date') or '', reverse=True)
    return merged


def list_ledger_documents(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    local_only: bool = False,
) -> dict:
    start, end = _default_range(start_date, end_date)
    tranzila_rows = []
    source = 'local'
    error = None
    local_formal = _local_formal_rows(start, end)
    if not local_only:
        try:
            service = _tranzila_client()
            result = service.list_documents(start, end)
            if result.get('success'):
                local_by_id = {
                    row['tranzila_doc_id']: row
                    for row in local_formal
                    if row.get('tranzila_doc_id')
                }
                local_by_number = {row['document_number']: row for row in local_formal}
                for raw in result.get('documents') or []:
                    match = local_by_id.get(str(raw.get('id') or '')) or local_by_number.get(
                        str(raw.get('number') or '')
                    )
                    customer = match['customer_name'] if match else ''
                    tranzila_rows.append(normalize_tranzila_document(raw, customer_name=customer))
                source = 'tranzila'
            else:
                error = result.get('error')
                logger.warning('Tranzila get_documents unavailable: %s', error)
        except Exception as exc:
            error = str(exc)
            logger.exception('Tranzila get_documents failed')

    documents = _merge_documents(
        tranzila_rows,
        local_formal,
        _local_crm_invoice_rows(start, end),
        _local_store_invoice_rows(start, end),
    )
    return {
        'documents': documents,
        'source': source,
        'error': error,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
    }


def _local_payment_rows(start: date, end: date) -> list[dict]:
    from apps.customers.models import Payment
    from apps.store.models import StoreInvoice

    rows = []
    payments = (
        Payment.objects
        .select_related('child', 'family', 'tranzila_transaction', 'branch')
        .prefetch_related('invoices')
        .filter(created_at__date__gte=start, created_at__date__lte=end)
        .filter(final_amount__gt=0)
        .filter(
            Q(tranzila_transaction__isnull=False)
            | Q(status__in=('completed', 'failed', 'processing', 'pending'))
        )
        .order_by('-created_at')
    )
    for payment in payments:
        txn = payment.tranzila_transaction
        invoice_number = ''
        related_invoices = list(payment.invoices.all())
        if related_invoices:
            related_invoices.sort(key=lambda inv: inv.invoice_date or timezone.now(), reverse=True)
            invoice_number = related_invoices[0].invoice_number
        rows.append({
            'id': str(payment.id),
            'created_at': _iso_datetime(payment.payment_date or payment.created_at),
            'customer_name': payment.child.full_name if payment.child_id else (payment.family.name if payment.family_id else ''),
            'invoice_number': invoice_number or (txn.transaction_id if txn else ''),
            'amount': _parse_amount(payment.final_amount),
            'payment_method': 'אשראי',
            'transaction_reference': (
                (txn.confirmation_code or txn.transaction_id) if txn else ''
            ),
            'status': payment.status,
            'card_last4': '',
            'source': 'widget',
        })

    store = (
        StoreInvoice.objects
        .select_related('child', 'tranzila_txn')
        .filter(issue_date__date__gte=start, issue_date__date__lte=end)
        .filter(total_amount__gt=0)
    )
    for inv in store:
        txn = inv.tranzila_txn
        rows.append({
            'id': f'store-{inv.id}',
            'created_at': _iso_datetime(inv.issue_date),
            'customer_name': inv.child.full_name if inv.child_id else (inv.customer_name or ''),
            'invoice_number': inv.invoice_number,
            'amount': _parse_amount(inv.total_amount),
            'payment_method': PAYMENT_METHOD_LABELS.get(inv.payment_method, inv.payment_method),
            'transaction_reference': (
                inv.tranzila_confirmation_code
                or inv.tranzila_transaction_id
                or (txn.confirmation_code if txn else '')
            ),
            'status': inv.payment_status,
            'card_last4': '',
            'source': 'store',
        })

    rows.sort(key=lambda row: row.get('created_at') or '', reverse=True)
    return rows


def list_ledger_payments(start_date: Optional[date] = None, end_date: Optional[date] = None) -> dict:
    start, end = _default_range(start_date, end_date)
    payments = _local_payment_rows(start, end)
    return {
        'payments': payments,
        'source': 'local',
        'error': None,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
    }
