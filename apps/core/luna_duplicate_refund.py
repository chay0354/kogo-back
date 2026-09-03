"""One-off refund for Luna Shoker's duplicate widget charges.

Refunds only the two extra payments. Leaves the original registration,
September monthly charge, and the first standing order untouched.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from apps.core.vat import split_vat_inclusive

CHILD_ID = 'e1cd416c-b72e-4f89-8dfb-623b5bbde913'
KEEP_STO_ID = '5ef7df79-4c90-4d26-a499-fd1a8d1743a9'
CANCEL_STO_ID = '56f29bcd-97d3-42ea-b108-9e8fbbd376e2'
KEEP_PAYMENT_IDS = frozenset((
    '6ec6417a-cd2a-4f2c-b72b-cb146502e448',
    '65e7e402-a130-4f2a-960d-33f872304002',
))
REFUND_PAYMENTS = (
    ('7be016ca-fa7a-4360-b903-bf7c36defacd', Decimal('120.00'), 'זיכוי חיוב כפול — דמי רישום'),
    ('707e50cf-ec84-4223-a9f5-3eb3dc8e18e6', Decimal('225.00'), 'זיכוי חיוב כפול — מנוי ספטמבר'),
)
CREDIT_NOTE_MARKER = 'Luna Shoker widget double signup'
CREDIT_NOTE_MARKER_REG = 'Luna Shoker widget double signup — registration 7be016ca'


def run_luna_duplicate_refund() -> dict:
    from apps.core.payment_service import PaymentService
    from apps.customers.models import Payment, RecurringPayment

    keep_sto = RecurringPayment.objects.get(id=KEEP_STO_ID)
    cancel_sto = RecurringPayment.objects.get(id=CANCEL_STO_ID)
    if str(keep_sto.child_id) != CHILD_ID or str(cancel_sto.child_id) != CHILD_ID:
        raise ValueError('הוראת הקבע אינה של לונה שוקר')
    if keep_sto.status != 'active':
        raise ValueError('הוראת הקבע התקינה אינה פעילה — נעצרתי')

    svc = PaymentService()
    refunds = []
    refunded_total = Decimal('0.00')

    for payment_id, expected, reason in REFUND_PAYMENTS:
        if payment_id in KEEP_PAYMENT_IDS:
            raise ValueError('ניסיון לזכות חיוב תקין נחסם')
        payment = Payment.objects.select_related('tranzila_transaction', 'child').get(id=payment_id)
        if str(payment.child_id) != CHILD_ID:
            raise ValueError('התשלום אינו של לונה שוקר')
        if payment.final_amount != expected:
            raise ValueError(f'סכום לא צפוי לתשלום {payment_id}')

        if payment.status == 'refunded':
            refunds.append({
                'payment_id': payment_id,
                'amount': float(expected),
                'success': True,
                'already_refunded': True,
            })
            refunded_total += expected
            continue

        if payment.status != 'completed':
            refunds.append({
                'payment_id': payment_id,
                'amount': float(expected),
                'success': False,
                'error': f'סטטוס {payment.status}',
            })
            continue

        result = svc.refund_payment(payment_id, reason=reason)
        if not result.get('success'):
            result = _retry_with_keep_card(svc, payment, keep_sto, reason)
        refunds.append({
            'payment_id': payment_id,
            'amount': float(expected),
            'success': bool(result.get('success')),
            'error': result.get('error'),
            'transaction_id': result.get('transaction_id', ''),
        })
        if result.get('success'):
            refunded_total += expected

    if cancel_sto.status != 'cancelled':
        cancel_sto.status = 'cancelled'
        cancel_sto.cancelled_at = timezone.now()
        cancel_sto.cancellation_reason = 'ביטול הוראת קבע כפולה — נרשמה פעמיים בטופס (לונה שוקר)'
        cancel_sto.save(update_fields=['status', 'cancelled_at', 'cancellation_reason'])

    keep_sto.refresh_from_db()
    if keep_sto.status != 'active':
        raise ValueError('הוראת הקבע התקינה בוטלה בטעות — בדוק מייד')

    credits = []
    monthly_ok = any(row['payment_id'] == REFUND_PAYMENTS[1][0] and row['success'] for row in refunds)
    registration_ok = any(row['payment_id'] == REFUND_PAYMENTS[0][0] and row['success'] for row in refunds)

    if monthly_ok:
        credits.append(_ensure_credit_invoice(
            amount=Decimal('225.00'),
            marker=CREDIT_NOTE_MARKER,
            linked_invoice='INV-20260901-707E50CF',
            description='זיכוי מנוי חודשי כפול ספטמבר — מחול גיל רך 4.5-6 — לונה שוקר',
            reason='זיכוי מנוי ספטמבר הכפול. החשבונית התקינה נשארת בתוקף.',
        ))
    if registration_ok:
        credits.append(_ensure_credit_invoice(
            amount=Decimal('120.00'),
            marker=CREDIT_NOTE_MARKER_REG,
            linked_invoice='INV-20260830-FAM-7BE016CA',
            description='זיכוי דמי רישום כפול — מחול גיל רך 4.5-6 — לונה שוקר',
            reason='זיכוי דמי רישום כפול. החשבונית התקינה מ-19.8 נשארת בתוקף.',
        ))

    return {
        'success': all(row['success'] for row in refunds) and refunded_total > 0,
        'refunded_total': float(refunded_total),
        'refunds': refunds,
        'duplicate_sto_cancelled': True,
        'keep_sto_id': KEEP_STO_ID,
        'keep_sto_next_billing_date': str(keep_sto.next_billing_date or ''),
        'credit_invoice': credits[-1] if credits else None,
        'credit_invoices': credits,
    }


def _ensure_credit_invoice(*, amount, marker, linked_invoice, description, reason):
    from apps.customers.financial_models import Invoice
    from apps.documents.models import DocumentLineItem, FormalDocument
    from apps.documents.service import create_credit_invoice

    existing = FormalDocument.objects.filter(
        child_id=CHILD_ID,
        document_type='credit_invoice',
        internal_notes=marker,
    ).order_by('-created_at').first()
    if existing:
        return {
            'id': str(existing.id),
            'document_number': existing.document_number,
            'total_amount': float(existing.total_amount),
            'already_existed': True,
        }

    before_vat, _vat, _gross = split_vat_inclusive(amount)
    doc = create_credit_invoice({
        'client_type': 'existing',
        'child_id': CHILD_ID,
        'credit_invoice_details': {
            'document_date': date.today(),
            'linked_invoice_id': linked_invoice,
            'credit_reason': reason,
            'credit_amount_before_vat': str(before_vat),
            'customer_notes': reason,
            'internal_notes': marker,
        },
    })
    doc.description = description
    doc.save(update_fields=['description'])
    DocumentLineItem.objects.create(
        document=doc,
        description=description,
        quantity=1,
        unit_price=before_vat,
    )
    inv = Invoice.objects.filter(invoice_number=linked_invoice).first()
    if inv and marker not in (inv.admin_notes or ''):
        inv.admin_notes = ((inv.admin_notes or '') + f'\nזוכה בחשבונית זיכוי {doc.document_number}').strip()
        inv.save(update_fields=['admin_notes', 'updated_at'])
    return {
        'id': str(doc.id),
        'document_number': doc.document_number,
        'total_amount': float(doc.total_amount),
        'already_existed': False,
    }


def _retry_with_keep_card(svc, payment, keep_sto, reason: str) -> dict:
    from apps.core.payment_service import terminal_for_payment_refund
    from apps.customers.models import TranzilaTransaction

    txn = payment.tranzila_transaction
    if not txn:
        return {'success': False, 'error': 'אין עסקת טרנזילה'}
    retry = svc.tranzila_service.refund_transaction(
        transaction_id=txn.transaction_id,
        amount=payment.final_amount,
        reason=reason,
        authorization_number=txn.confirmation_code,
        card_expire_month=keep_sto.card_expire_month,
        card_expire_year=keep_sto.card_expire_year,
        token=keep_sto.tranzila_token,
        prefer_cancel=False,
        terminal_name=terminal_for_payment_refund(payment),
    )
    if not retry.get('success'):
        return retry
    TranzilaTransaction.objects.create(
        transaction_id=retry.get('transaction_id', ''),
        confirmation_code=retry.get('confirmation_code', ''),
        transaction_type='refund',
        response_code=retry.get('response_code', '000'),
        response_message=retry.get('message', ''),
        request_data={
            'original_transaction_id': txn.transaction_id,
            'authorization_number': txn.confirmation_code,
            'amount': str(payment.final_amount),
            'reason': reason,
        },
        response_data=retry.get('raw_response', {}),
        idempotency_key=f'refund_payment_{payment.id}_{retry.get("transaction_id", "")}',
        is_successful=True,
        response_timestamp=timezone.now(),
    )
    payment.status = 'refunded'
    payment.save(update_fields=['status', 'updated_at'])
    retry['refund_amount'] = float(payment.final_amount)
    return retry
