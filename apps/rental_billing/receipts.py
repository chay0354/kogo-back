"""The חשבונית מס/קבלה for a tenant's charge — a FormalDocument numbered in the RT run.

Why a FormalDocument: it already names a business customer (the tenant), the
income tags and a branch, breaks the VAT out, keeps the lines and how it was
paid; the documents register, the period report, the uniform export and
continuity() all read it; and its PDF carries the marks (מקור, מסמך ממוחשב, and
for this run the issuer's עוסק מורשה line). A run of its own (RT, numbering.py)
keeps the rentals' receipts checkable on their own, as IR does for lessons.

It is not sent to Tranzila's document API (service._attempt_tranzila): that
would be a second document, numbered by Tranzila, for the same charge. The
lesson receipts are not sent either.

One receipt per charge: the charge row is locked and its receipt link checked
inside the transaction that takes the number, so issuing twice returns the
first one. The number is drawn in that same transaction, so a failure gives it
back and the run stays gapless.

How the month was paid is the receipt's payment line. A card charge writes a
credit-card line, as it always did. A month the tenant paid at the office
(offline.py: cash, a check, a bank transfer) writes that means instead, with
the check's details — and that line is what the signing package reads to
decide where the signed original goes (הוראה 18ב(ד): cash or an unmarked check
on paper, a crossed check or a transfer by mail). Numbering, signing and the
mail are the same for both.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.vat import VAT_PERCENT_DISPLAY
from apps.documents.models import DocumentLineItem, DocumentPayment, FormalDocument
from apps.documents.numbering import SERIES_RENTAL, next_document_number
from apps.rental_billing.billing import shekels
from apps.rental_billing.models import TenantCharge
from apps.rental_billing.schedule import month_label

logger = logging.getLogger(__name__)

LINE_LABEL = 'שכירות סטודיו'

# The means a tenant may pay a month with at the office. A card is never one of
# them: a card is charged through Tranzila, on the order's own rails.
METHOD_CASH = 'cash'
METHOD_CHECK = 'check'
METHOD_BANK_TRANSFER = 'bank_transfer'
OFFLINE_METHODS = (METHOD_CASH, METHOD_CHECK, METHOD_BANK_TRANSFER)
OFFLINE_METHOD_LABELS = {
    METHOD_CASH: 'מזומן',
    METHOD_CHECK: "צ'ק",
    METHOD_BANK_TRANSFER: 'העברה בנקאית',
}


class ReceiptError(ValueError):
    pass


@dataclass(frozen=True)
class OfflinePayment:
    """How a tenant paid a month at the office, as its receipt names it. Checked by offline.py before it gets here."""

    method: str
    # The day the money reached the office: the cash taken, the check handed
    # over, the transfer's value date.
    paid_on: date
    # The receipt book's number, the transfer's reference — whatever the office has.
    reference: str = ''
    # A check (הוראה 5(ב)): its number, bank, branch, account and due date,
    # and whether it is crossed "לא סחיר" in the tenant's name (הוראה 18ב(ד)(2)).
    check_number: str = ''
    check_bank: str = ''
    check_branch: str = ''
    check_account: str = ''
    check_date: date | None = None
    check_crossed: bool = False

    @property
    def label(self) -> str:
        return OFFLINE_METHOD_LABELS.get(self.method, self.method)


def lock_charge(charge_id) -> TenantCharge:
    return (
        TenantCharge.objects.select_for_update(of=('self',))
        .select_related('standing_order', 'standing_order__tenant', 'standing_order__branch', 'receipt')
        .get(pk=charge_id)
    )


def issue_receipt(charge_id, *, payment: OfflinePayment | None = None, user=None) -> FormalDocument:
    """The charge's receipt, issued now or the one it already has. E-mailed to the tenant after commit."""
    with transaction.atomic(durable=True):
        return issue_receipt_in_transaction(lock_charge(charge_id), payment=payment, user=user)


def issue_receipt_in_transaction(charge: TenantCharge, *, payment: OfflinePayment | None = None,
                                 user=None) -> FormalDocument:
    """
    issue_receipt's work, inside the caller's transaction — for a caller that
    changes the charge and must issue its receipt in the same commit (a month
    paid at the office). `charge` must be locked by that transaction.

    `payment` None is a card charge. With one, the receipt's payment line is
    that means, and the late note dates the payment by its paid_on.
    """
    if charge.status != TenantCharge.STATUS_CHARGED:
        raise ReceiptError('קבלה מופקת רק לחיוב שעבר')
    if charge.receipt_id:
        return charge.receipt
    if payment is not None and payment.method not in OFFLINE_METHODS:
        raise ReceiptError('אמצעי תשלום לא מוכר')

    order = charge.standing_order
    tenant = order.tenant
    # Dated the day it is issued, and numbered from that day's year: a
    # receipt issued after the charge day (a retry of a failed receipt, the
    # office's issue-receipt) must not carry an earlier date than documents
    # already numbered before it. Issued late, it says so, with both dates,
    # as apps/documents/missing_receipts.py marks a late lesson receipt.
    issued_at = timezone.now()
    issued_on = timezone.localdate(issued_at)
    if payment is not None:
        charged_on = payment.paid_on
    else:
        charged_on = timezone.localdate(charge.charged_at) if charge.charged_at else issued_on
    late_note = ''
    if charged_on != issued_on:
        late_note = (
            f'הופק באיחור · התשלום התקבל ב־{charged_on:%d/%m/%Y} · המסמך הופק ב־{issued_on:%d/%m/%Y}'
        )
    period = month_label(charge.period)
    net = shekels(charge.amount_before_vat)
    vat = shekels(charge.vat_amount)
    total = shekels(charge.total)
    line = f'{LINE_LABEL} · {period}' + (f' · {order.branch.name}' if order.branch_id else '')
    origin = (
        f'הופק אוטומטית עם חיוב שכירות {charge.pk}' if payment is None
        else f'הופק במשרד עם רישום תשלום ב{payment.label} לחיוב שכירות {charge.pk}'
    )

    doc = FormalDocument.objects.create(
        document_number=next_document_number(SERIES_RENTAL, issued_at),
        document_type='combined',
        client_type='business',
        business_customer=tenant,
        customer_name=tenant.full_name or None,
        business_id=charge.business_id,
        business_category_id=charge.business_category_id,
        branch_id=order.branch_id,
        document_date=issued_on,
        description=f'{LINE_LABEL} לחודש {period}',
        currency='ILS',
        prices_include_vat=False,
        vat_exempt=False,
        vat_percent=VAT_PERCENT_DISPLAY,
        subtotal=net,
        discount_amount=Decimal('0'),
        discount_percent=Decimal('0'),
        vat_amount=vat,
        total_amount=total,
        # Printed on the receipt (the payment panel) and kept for the office.
        customer_notes=late_note,
        internal_notes=origin + (f' · {late_note}' if late_note else ''),
        issued_at=issued_at,
        issued_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    DocumentLineItem.objects.create(
        document=doc, description=line[:500], quantity=Decimal('1'), unit_price=net,
    )
    if payment is None:
        _card_payment_line(doc, charge, total, charged_on)
    else:
        _offline_payment_line(doc, payment, total)
    TenantCharge.objects.filter(pk=charge.pk).update(receipt=doc, receipt_error='', updated_at=timezone.now())

    # The signed original's row commits with the receipt; it is signed
    # after the commit, before the mail below attaches it (off: a no-op).
    from apps.documents.models import SignedOriginal
    from apps.documents.signing.service import KIND_FORMAL, issue as issue_signed_original

    issue_signed_original(
        KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL, email_to=(tenant.email or '').strip(),
    )

    doc_id = doc.pk
    number = doc.document_number

    def _email():
        # After the commit, never before: a mail sent from a transaction that
        # then rolled back would carry a number the run hands out again.
        from apps.rental_billing.receipt_email import send_rental_receipt_email

        try:
            send_rental_receipt_email(doc_id)
        except Exception:
            logger.exception('Rental receipt email failed for %s (non-fatal)', number)

    transaction.on_commit(_email)
    logger.info('Rental receipt %s issued for charge %s', number, charge.pk)
    return doc


def _card_payment_line(doc: FormalDocument, charge: TenantCharge, total: Decimal, charged_on: date) -> None:
    # What the office looks the charge up by in Tranzila. A charge that
    # carries neither (one the office marked charged without a code) says so
    # rather than printing "עסקה " with nothing after it.
    confirmation = charge.confirmation_code or ''
    reference = (
        f'אישור {confirmation}' if confirmation
        else (f'עסקה {charge.transaction_id}' if charge.transaction_id else 'חיוב בכרטיס אשראי')
    )
    DocumentPayment.objects.create(
        document=doc,
        payment_method='credit_card',
        amount=total,
        card_last_four=(charge.card_last4 or '')[:4],
        card_installments=1,
        reference=reference[:200],
        notes=f'טרנזילה · עסקה {charge.transaction_id}' if charge.transaction_id else '',
        paid_on=charged_on,
    )


def _offline_payment_line(doc: FormalDocument, payment: OfflinePayment, total: Decimal) -> None:
    """The means the office recorded, for the whole month. A check carries its details — printed on the receipt."""
    fields = {
        'document': doc,
        'payment_method': payment.method,
        'amount': total,
        'paid_on': payment.paid_on,
        'notes': 'נרשם במשרד',
    }
    if payment.method == METHOD_CHECK:
        # The check's number is the payment's reference, as a hand-issued
        # receipt keeps it (apps/documents/service.py) and the uniform export reads it.
        fields.update(
            reference=payment.check_number[:200],
            check_date=payment.check_date,
            check_bank=payment.check_bank[:100],
            check_branch=payment.check_branch[:50],
            check_account=payment.check_account[:50],
            check_crossed=bool(payment.check_crossed),
        )
    else:
        fields.update(reference=payment.reference[:200], check_crossed=False)
    DocumentPayment.objects.create(**fields)
