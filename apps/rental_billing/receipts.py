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
"""
from __future__ import annotations

import logging
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


class ReceiptError(ValueError):
    pass


def issue_receipt(charge_id) -> FormalDocument:
    """The charge's receipt, issued now or the one it already has. E-mailed to the tenant after commit."""
    with transaction.atomic(durable=True):
        charge = (
            TenantCharge.objects.select_for_update(of=('self',))
            .select_related('standing_order', 'standing_order__tenant', 'standing_order__branch', 'receipt')
            .get(pk=charge_id)
        )
        if charge.status != TenantCharge.STATUS_CHARGED:
            raise ReceiptError('קבלה מופקת רק לחיוב שעבר')
        if charge.receipt_id:
            return charge.receipt

        order = charge.standing_order
        tenant = order.tenant
        # Dated the day it is issued, and numbered from that day's year: a
        # receipt issued after the charge day (a retry of a failed receipt, the
        # office's issue-receipt) must not carry an earlier date than documents
        # already numbered before it. Issued late, it says so, with both dates,
        # as apps/documents/missing_receipts.py marks a late lesson receipt.
        issued_at = timezone.now()
        issued_on = timezone.localdate(issued_at)
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
            internal_notes=f'הופק אוטומטית עם חיוב שכירות {charge.pk}' + (f' · {late_note}' if late_note else ''),
        )
        DocumentLineItem.objects.create(
            document=doc, description=line[:500], quantity=Decimal('1'), unit_price=net,
        )
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
        )
        TenantCharge.objects.filter(pk=charge.pk).update(receipt=doc, receipt_error='', updated_at=timezone.now())

        # The signed original's row commits with the receipt; it is signed
        # after the commit, before the mail below attaches it (off: a no-op).
        from apps.documents.models import SignedOriginal
        from apps.documents.signing.service import KIND_FORMAL, issue as issue_signed_original

        issue_signed_original(
            KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL, email_to=(tenant.email or '').strip(),
        )

        doc_id = doc.pk

        def _email():
            # After the commit, never before: a mail sent from a transaction that
            # then rolled back would carry a number the run hands out again.
            from apps.rental_billing.receipt_email import send_rental_receipt_email

            try:
                send_rental_receipt_email(doc_id)
            except Exception:
                logger.exception('Rental receipt email failed for %s (non-fatal)', doc.document_number)

        transaction.on_commit(_email)
    logger.info('Rental receipt %s issued for charge %s', doc.document_number, charge_id)
    return doc
