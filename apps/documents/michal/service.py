"""
Issue the documents Michal Kagan's site asks for.

A payment gets a חשבונית מס/קבלה in the MK run; a refund gets a חשבונית מס זיכוי
in the CR run, pointing at the document it credits. Both are built here rather
than through service.create_combined, as the rental receipts are
(apps/rental_billing/receipts.py): that path reads payment methods as Hebrew
labels (a card would be filed as cash), keeps no card digits or reference, and
sends the document to Tranzila's document API, which would be a second,
Tranzila-numbered document for the same payment.

Each request carries her site's own id for the payment or the refund. Asking
twice gives the first document back: the id is marked on the document
(internal_notes), and the check and the numbering happen under a transaction
lock on that id, so two requests at once cannot both issue. The number is drawn
in the same transaction, so a failure gives it back and the run stays gapless.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.vat import VAT_PERCENT_DISPLAY, split_vat_inclusive
from apps.documents.models import DocumentLineItem, DocumentPayment, FormalDocument, SignedOriginal
from apps.documents.numbering import SERIES_CREDIT, SERIES_MICHAL, next_document_number

logger = logging.getLogger(__name__)

# Marks on the records, carrying her site's ids. The first line of a document's
# internal notes, and a line of a business customer's notes.
PAYMENT_MARK = 'michal-payment:'
REFUND_MARK = 'michal-refund:'
CLIENT_MARK = 'michal-client:'

DEFAULT_LINE = 'טיפול קונדליני אקטיביישן'

# How her site's payment methods read on the document. All are taken by Tranzila
# on her hosted page and settle as card transactions.
METHOD_LABELS = {
    'credit_card': 'כרטיס אשראי',
    'bit': 'Bit',
    'apple_pay': 'Apple Pay',
    'google_pay': 'Google Pay',
}


class MichalDocumentError(ValueError):
    status = 400


class NotConfigured(MichalDocumentError):
    """Her Business is missing: documents are refused, never filed under another."""
    status = 503


class OriginalMissing(MichalDocumentError):
    """A refund of a payment kogo issued no document for (yet): her site asks again later."""
    status = 409


@dataclass(frozen=True)
class Issued:
    document: FormalDocument
    created: bool


def michal_business():
    """Her Business and its first active category. Looked up by name, never created here."""
    from apps.core.models import Business, BusinessCategory

    name = (getattr(settings, 'MICHAL_BUSINESS_NAME', '') or '').strip()
    business = Business.objects.filter(name=name).first() if name else None
    if business is None:
        raise NotConfigured(f'העסק "{name}" לא קיים במערכת. יש ליצור אותו בהגדרות → כספים → עסקים.')
    category = (
        BusinessCategory.objects.filter(business=business, is_active=True).order_by('sort_order', 'name').first()
    )
    return business, category


def _lock(key: str) -> None:
    """Serialise everything done for one of her ids until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [key])


def _issued(document_type: str, mark: str):
    return (
        FormalDocument.objects.select_related('business_customer')
        .filter(document_type=document_type, internal_notes__startswith=f'{mark}\n')
        .first()
    )


def _split_name(full_name: str) -> tuple[str, str]:
    first, _, last = (full_name or '').strip().partition(' ')
    return (first.strip() or 'לקוח/ה')[:100], last.strip()[:100]


def find_or_create_customer(business, category, client: dict):
    """
    Her client as a business customer of her Business: found by her site's id,
    kept up to date, created the first time. Never a Family or a Child.
    """
    from apps.customers.models import BusinessCustomer

    mark = f'{CLIENT_MARK}{client["external_id"]}'
    customer = (
        BusinessCustomer.objects.select_for_update()
        .filter(business=business, notes__contains=mark)
        .first()
    )
    first_name, last_name = _split_name(client['full_name'])
    values = {
        'first_name': first_name,
        'last_name': last_name,
        'email': (client.get('email') or '').strip(),
        'phone': (client.get('phone') or '').strip()[:20],
    }
    if customer is None:
        customer = BusinessCustomer.objects.create(
            **values,
            business=business,
            business_category=category,
            notes=f'{mark}\nלקוח/ה של מיכל קגן. נוצר אוטומטית מהאתר שלה.',
        )
    else:
        # A value her site did not send (an empty e-mail) never wipes one kogo holds.
        changed = [field for field, value in values.items() if value and getattr(customer, field) != value]
        for field in changed:
            setattr(customer, field, values[field])
        if changed:
            customer.save(update_fields=[*changed, 'updated_at'])

    if client.get('computerized_docs_consent') is True:
        from apps.core.computerized_docs import CONSENT_SOURCE_WEBSITE, record_consent

        record_consent(customer, CONSENT_SOURCE_WEBSITE)
    return customer


def _signed_and_mailed(doc: FormalDocument, email: str, name: str) -> None:
    """Record the signed original with the document, and mail it after the commit."""
    from apps.documents.signing.service import KIND_FORMAL, issue as issue_signed_original

    issue_signed_original(
        KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_MICHAL, email_to=email, customer_name=name,
    )
    doc_id, number = doc.pk, doc.document_number

    def _email():
        # After the commit, never before: a mail sent from a transaction that
        # then rolled back would carry a number the run hands out again.
        from apps.documents.michal.email import send_michal_document_email

        try:
            send_michal_document_email(doc_id)
        except Exception:
            logger.exception('Michal document email failed for %s (non-fatal; the signing cron retries)', number)

    transaction.on_commit(_email)


def issue_payment_document(data: dict) -> Issued:
    """The חשבונית מס/קבלה of one of her payments: issued now, or the one already issued."""
    business, category = michal_business()
    payment = data['payment']
    mark = f'{PAYMENT_MARK}{data["external_id"]}'

    with transaction.atomic(durable=True):
        _lock(mark)
        existing = _issued('combined', mark)
        if existing is not None:
            return Issued(existing, False)

        customer = find_or_create_customer(business, category, data['customer'])

        # Dated the day it is issued and numbered from that day's year, as the
        # rental receipts are: a document issued after the payment day must not
        # carry an earlier date than documents already numbered before it.
        issued_at = timezone.now()
        issued_on = timezone.localdate(issued_at)
        paid_at = payment['paid_at']
        paid_on = timezone.localdate(paid_at) if timezone.is_aware(paid_at) else paid_at.date()
        late_note = ''
        if paid_on < issued_on:
            late_note = f'הופק באיחור · התשלום התקבל ב־{paid_on:%d/%m/%Y} · המסמך הופק ב־{issued_on:%d/%m/%Y}'

        net, vat, total = split_vat_inclusive(payment['amount'])
        description = (payment.get('description') or DEFAULT_LINE).strip()[:300]
        line = f'{description} · מיכל קגן'
        if payment.get('session_date'):
            line += f' · {payment["session_date"]:%d/%m/%Y}'

        transaction_id = (payment.get('transaction_id') or '').strip()
        confirmation = (payment.get('confirmation_code') or '').strip()
        method = payment.get('method') or 'credit_card'

        doc = FormalDocument.objects.create(
            document_number=next_document_number(SERIES_MICHAL, issued_at),
            document_type='combined',
            client_type='business',
            business_customer=customer,
            customer_name=customer.full_name or None,
            business=business,
            business_category=category,
            branch=None,
            document_date=issued_on,
            description=description,
            currency='ILS',
            prices_include_vat=False,
            vat_exempt=False,
            vat_percent=VAT_PERCENT_DISPLAY,
            subtotal=net,
            discount_amount=Decimal('0'),
            discount_percent=Decimal('0'),
            vat_amount=vat,
            total_amount=total,
            customer_notes=late_note,
            internal_notes=(
                f'{mark}\nהופק אוטומטית לבקשת האתר של מיכל קגן'
                + (f' · עסקה {transaction_id}' if transaction_id else '')
                + (f' · {late_note}' if late_note else '')
            ),
        )
        DocumentLineItem.objects.create(document=doc, description=line[:500], quantity=Decimal('1'), unit_price=net)
        reference = (
            f'אישור {confirmation}' if confirmation
            else (f'עסקה {transaction_id}' if transaction_id else 'חיוב בכרטיס אשראי')
        )
        DocumentPayment.objects.create(
            document=doc,
            payment_method='credit_card',
            amount=total,
            card_last_four=(payment.get('card_last_four') or '')[:4],
            card_installments=1,
            reference=reference[:200],
            notes=(
                f'{METHOD_LABELS.get(method, METHOD_LABELS["credit_card"])} · טרנזילה'
                + (f' · עסקה {transaction_id}' if transaction_id else '')
            )[:500],
        )
        _signed_and_mailed(doc, (customer.email or '').strip(), customer.full_name)

    logger.info('Michal document %s issued for payment %s', doc.document_number, data['external_id'])
    return Issued(doc, True)


def issue_refund_credit(data: dict) -> Issued:
    """The חשבונית מס זיכוי of one of her refunds, pointing at the document it credits."""
    michal_business()  # refuses while her Business is missing, as payments do
    refund = data['refund']
    mark = f'{REFUND_MARK}{data["external_id"]}'
    original_mark = f'{PAYMENT_MARK}{refund["payment_external_id"]}'

    with transaction.atomic(durable=True):
        _lock(mark)
        existing = _issued('credit_invoice', mark)
        if existing is not None:
            return Issued(existing, False)

        # Every credit against one document is decided under that document's
        # lock, so two refunds at once cannot credit more than was paid.
        _lock(original_mark)
        original = _issued('combined', original_mark)
        if original is None:
            raise OriginalMissing('לתשלום הזה עוד לא הופק מסמך, ולכן אין מה לזכות.')

        amount = Decimal(str(refund['amount']))
        already = (
            FormalDocument.objects.filter(document_type='credit_invoice', linked_document=original)
            .aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        )
        if already + amount > original.total_amount:
            raise MichalDocumentError(
                f'הזיכוי ({amount}) גדול מהיתרה שלא זוכתה ({original.total_amount - already}) במסמך {original.document_number}.'
            )

        net, vat, total = split_vat_inclusive(amount)
        customer = original.business_customer
        reason = (refund.get('reason') or '').strip()[:500] or 'החזר כספי'
        doc = FormalDocument.objects.create(
            document_number=next_document_number(SERIES_CREDIT),
            document_type='credit_invoice',
            client_type='business',
            business_customer=customer,
            customer_name=original.customer_name,
            business_id=original.business_id,
            business_category_id=original.business_category_id,
            branch=None,
            document_date=timezone.localdate(),
            vat_exempt=False,
            vat_percent=VAT_PERCENT_DISPLAY,
            subtotal=net,
            discount_amount=Decimal('0'),
            discount_percent=Decimal('0'),
            vat_amount=vat,
            total_amount=total,
            linked_document=original,
            linked_document_number=original.document_number,
            linked_document_date=original.document_date,
            credit_reason=reason,
            internal_notes=f'{mark}\nהופק אוטומטית עם החזר באתר של מיכל קגן',
        )
        email = (customer.email or '').strip() if customer else ''
        name = customer.full_name if customer else (original.customer_name or '')
        _signed_and_mailed(doc, email, name)

    logger.info('Michal credit note %s issued for refund %s', doc.document_number, data['external_id'])
    return Issued(doc, True)


def michal_document(number: str):
    """One of her documents by number, or None. Never a document of another business line."""
    return (
        FormalDocument.objects.select_related('business_customer', 'business')
        .filter(document_number=number)
        .filter(internal_notes__regex=rf'^({PAYMENT_MARK}|{REFUND_MARK})')
        .first()
    )
