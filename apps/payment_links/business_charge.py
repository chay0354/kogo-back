"""A verified Cogolive payment becomes exactly one business tax document."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction

from apps.documents import service as document_service
from apps.documents.numbering import israel_today
from apps.documents.settlement import balance_of, settle_on_issue
from apps.payment_links.models import PaymentLink, PaymentLinkPayment, money

logger = logging.getLogger(__name__)


class BusinessChargeDocumentError(ValueError):
    pass


def target_invoice_balance(link: PaymentLink) -> Decimal | None:
    if not link.target_invoice_id:
        return None
    line = balance_of(link.target_invoice)
    return money(line.open) if line is not None else None


def validate_business_charge_link(link: PaymentLink, amount: Decimal) -> None:
    if link.kind != PaymentLink.KIND_BUSINESS_CHARGE:
        raise BusinessChargeDocumentError('זה אינו קישור גבייה עסקית')
    if not link.business_customer_id:
        raise BusinessChargeDocumentError('לא נבחר לקוח עסקי')
    if not link.business_id or not link.business_category_id:
        raise BusinessChargeDocumentError('יש לבחור עסק וקטגוריה')
    if link.business_category.business_id != link.business_id:
        raise BusinessChargeDocumentError('הקטגוריה אינה שייכת לעסק שנבחר')
    if link.target_invoice_id:
        invoice = link.target_invoice
        if invoice.business_customer_id != link.business_customer_id:
            raise BusinessChargeDocumentError('החשבונית שייכת ללקוח עסקי אחר')
        if invoice.document_type not in ('tax_invoice', 'transaction_invoice'):
            raise BusinessChargeDocumentError('אפשר לגבות רק חשבונית מס או חשבונית עסקה פתוחה')
        left = target_invoice_balance(link) or Decimal('0')
        if left <= 0:
            raise BusinessChargeDocumentError('החשבונית כבר סגורה')
        if money(amount) > left:
            raise BusinessChargeDocumentError(f'נותרו בחשבונית ₪{left}, פחות מסכום הגבייה')


def _common(link: PaymentLink) -> dict:
    return {
        'client_type': 'business',
        'business_customer_id': link.business_customer_id,
        'business_id': link.business_id,
        'business_category_id': link.business_category_id,
        'branch_id': link.branch_id,
    }


def _card_row(payment: PaymentLinkPayment) -> dict:
    return {
        'method': 'credit_card',
        'amount': payment.amount,
        'card_last_four': payment.card_last4,
        'card_brand': payment.card_type,
        'installments': 1,
        'reference': payment.gateway_confirmation_code,
        'paid_on': israel_today(),
    }


def _combined_data(payment: PaymentLinkPayment, description: str, *, settlement=None) -> dict:
    link = payment.link
    data = {
        **_common(link),
        'document_type': 'combined',
        'invoice_details': {
            'document_date': israel_today(),
            'description': description,
            'currency': 'ILS',
            'prices_include_vat': True,
            'line_items': [{'description': description, 'quantity': Decimal('1'), 'price': payment.amount}],
            'discount_amount': Decimal('0'),
            'discount_percent': Decimal('0'),
            'vat_exempt': False,
            'payment_terms': '',
            'customer_notes': '',
            'internal_notes': f'גבייה עסקית בקישור {link.id}',
            'payments': [_card_row(payment)],
            'withholding_amount': Decimal('0'),
        },
        'settlements': [],
    }
    if settlement is not None:
        data['settlements'] = [{'invoice_id': settlement.id, 'amount': payment.amount}]
    return data


def _receipt_data(payment: PaymentLinkPayment, invoice) -> dict:
    link = payment.link
    return {
        **_common(link),
        'document_type': 'receipt',
        'document_date': israel_today(),
        'receipt_details': {
            'payment_method': 'אשראי',
            'linked_invoice_id': invoice.document_number,
            'card_amount': payment.amount,
            'card_last_four': payment.card_last4,
            'card_brand': payment.card_type,
            'card_installments': 1,
            'card_notes': f'אישור {payment.gateway_confirmation_code}'.strip(),
        },
        'settlements': [{'invoice_id': invoice.id, 'amount': payment.amount}],
    }


@transaction.atomic
def issue_business_charge_document(payment_id) -> object | None:
    """Idempotently issue and link the document after money was verified."""
    payment = (
        PaymentLinkPayment.objects.select_for_update(of=('self',))
        .select_related(
            'link__business', 'link__business_category', 'link__business_customer',
            'link__target_invoice', 'formal_document',
        )
        .get(pk=payment_id)
    )
    if payment.formal_document_id:
        return payment.formal_document
    if payment.status != PaymentLinkPayment.STATUS_COMPLETED:
        return None

    link = payment.link
    validate_business_charge_link(link, payment.amount)
    description = (link.description or link.title or 'שירות')[:500]
    invoice = link.target_invoice
    if invoice is None:
        data = _combined_data(payment, description)
        doc = document_service.create_combined(data, issued_by=link.created_by)
    elif invoice.document_type == 'tax_invoice':
        data = _receipt_data(payment, invoice)
        doc = document_service.create_receipt(data, issued_by=link.created_by)
        settle_on_issue(doc, data, user=link.created_by)
    else:
        data = _combined_data(
            payment,
            f'תשלום עבור חשבונית עסקה {invoice.document_number} — {description}',
            settlement=invoice,
        )
        doc = document_service.create_combined(data, issued_by=link.created_by)
        settle_on_issue(doc, data, user=link.created_by)

    payment.formal_document = doc
    payment.document_error = ''
    payment.save(update_fields=['formal_document', 'document_error', 'updated_at'])
    link.is_active = False
    link.save(update_fields=['is_active', 'updated_at'])
    return doc


def ensure_business_charge_document(payment_id) -> None:
    """Never erase the verified charge when document issuance needs attention."""
    try:
        issue_business_charge_document(payment_id)
    except Exception as exc:
        logger.exception('business charge %s: automatic document issuance failed', payment_id)
        PaymentLinkPayment.objects.filter(pk=payment_id, formal_document__isnull=True).update(
            document_error=str(exc)[:1000],
        )
