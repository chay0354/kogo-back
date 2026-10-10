"""
A verified Cogolive payment becomes exactly one business tax document.

Which one depends on when the money came (owner, 1.10.2026):

* paid within BUSINESS_CHARGE_INVOICE_AFTER_HOURS of the link's creation —
  one חשבונית מס/קבלה;
* still unpaid after that — the business-invoices cron issues an open חשבונית
  מס for the same sum and ties it to the link (`target_invoice`); the payment
  that follows is a קבלה that closes it.

Locks. Whoever needs both rows takes the link's first and the payment's
second: the start view (link, then the attempts it retires and the row it
inserts), the cron (link only) and `issue_business_charge_document` (link,
then payment). The Tranzila callback holds a payment row alone and issues the
document after its own commit, so nothing waits in a circle. `target_invoice`
is written and read only under the link's lock — a payment completed at the
moment the cron invoices the link becomes a receipt on that invoice, never a
second tax document beside it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.customers.business_customer_location import is_branches_business
from apps.documents import service as document_service
from apps.documents.numbering import israel_today
from apps.documents.settlement import balance_of, settle_on_issue
from apps.payment_links.models import PaymentLink, PaymentLinkPayment, money

logger = logging.getLogger(__name__)

# How many overdue links one cron run looks at; `limit` bounds how many it invoices.
OVERDUE_SCAN_CAP = 200

_FINAL_STATUSES = (PaymentLinkPayment.STATUS_COMPLETED, PaymentLinkPayment.STATUS_REVIEW)


class BusinessChargeDocumentError(ValueError):
    pass


def invoice_after() -> timedelta:
    """How long a business charge may stay unpaid before it gets an open tax invoice."""
    return timedelta(hours=max(0, int(getattr(settings, 'BUSINESS_CHARGE_INVOICE_AFTER_HOURS', 24))))


def attempt_abandoned_after() -> timedelta:
    """How long a started payment is waited for before it stops blocking the link."""
    return timedelta(minutes=max(1, int(getattr(settings, 'BUSINESS_CHARGE_ATTEMPT_ABANDONED_MINUTES', 20))))


def invoice_links_from() -> datetime | None:
    """
    The moment the unpaid-after-a-day rule starts (BUSINESS_CHARGE_INVOICE_LINKS_FROM).

    None when it is empty. Raises ValueError when it is set and unreadable: a
    cron that cannot tell which links predate the rule issues nothing.
    """
    raw = str(getattr(settings, 'BUSINESS_CHARGE_INVOICE_LINKS_FROM', '') or '').strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError('BUSINESS_CHARGE_INVOICE_LINKS_FROM is not an ISO 8601 moment') from None
    if timezone.is_naive(moment):
        raise ValueError('BUSINESS_CHARGE_INVOICE_LINKS_FROM needs its UTC offset (e.g. +03:00)')
    return moment


def abandon_stale_attempts(link: PaymentLink, *, now=None) -> int:
    """
    Retire the payments the customer started on this link and never finished.

    Call it under the link's row lock. A pending row older than
    BUSINESS_CHARGE_ATTEMPT_ABANDONED_MINUTES becomes failed/'abandoned', so
    the link takes a new attempt. One conditional UPDATE: a row the callback is
    settling at this moment is waited for and then left as the callback wrote
    it. A late approved notify for a retired row is still read in full by the
    callback (it settles any row that is not completed) — the money is never
    dropped. Returns how many rows were retired.
    """
    now = now or timezone.now()
    return PaymentLinkPayment.objects.filter(
        link=link, status=PaymentLinkPayment.STATUS_PENDING, created_at__lte=now - attempt_abandoned_after(),
    ).update(
        status=PaymentLinkPayment.STATUS_FAILED, failure_reason=PaymentLinkPayment.REASON_ABANDONED,
        updated_at=timezone.now(),
    )


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
    if not link.business_id:
        raise BusinessChargeDocumentError('יש לבחור עסק וקטגוריה')
    if is_branches_business(link.business):
        # The branch files the charge; a category under סניפים is an extra.
        if not link.branch_id:
            raise BusinessChargeDocumentError('יש לבחור סניף')
    elif not link.business_category_id:
        raise BusinessChargeDocumentError('יש לבחור עסק וקטגוריה')
    if link.business_category_id and link.business_category.business_id != link.business_id:
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


# Tranzila's `cardtype` on the hosted page's notify (docs.tranzila.com, iframe
# integration). The payment row keeps the code as it arrived; a tax document
# names the card, it does not print "2".
TRANZILA_CARD_TYPES = {
    '1': 'מאסטרקארד',
    '2': 'ויזה',
    '3': 'דיינרס',
    '4': 'אמריקן אקספרס',
    '5': 'ישראכרט',
    '6': 'מאסטרו',
}


def card_brand_name(card_type: str) -> str:
    """The card's name for a document; an unknown code or a name passes through."""
    raw = str(card_type or '').strip()
    return TRANZILA_CARD_TYPES.get(raw, raw)


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
        'card_brand': card_brand_name(payment.card_type),
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
            'card_brand': card_brand_name(payment.card_type),
            'card_installments': 1,
            'card_notes': f'אישור {payment.gateway_confirmation_code}'.strip(),
        },
        'settlements': [{'invoice_id': invoice.id, 'amount': payment.amount}],
    }


def _invoice_data(link: PaymentLink, amount: Decimal, description: str) -> dict:
    """The open tax invoice of an unpaid link: the same sum, VAT inside it, the same line."""
    hours = int(invoice_after().total_seconds() // 3600)
    return {
        **_common(link),
        'document_type': 'tax_invoice',
        'invoice_details': {
            'document_date': israel_today(),
            'description': description,
            'currency': 'ILS',
            'prices_include_vat': True,
            'line_items': [{'description': description, 'quantity': Decimal('1'), 'price': amount}],
            'discount_amount': Decimal('0'),
            'discount_percent': Decimal('0'),
            'vat_exempt': False,
            'payment_terms': '',
            'customer_notes': '',
            'internal_notes': f'גבייה עסקית בקישור {link.id} — לא שולמה בתוך {hours} שעות מיצירת הקישור',
        },
    }


def _locked_link(link_id) -> PaymentLink:
    return (
        PaymentLink.objects.select_for_update(of=('self',))
        .select_related('business', 'business_category', 'business_customer', 'target_invoice', 'created_by')
        .get(pk=link_id)
    )


@transaction.atomic
def issue_business_charge_document(payment_id) -> object | None:
    """Idempotently issue and link the document after money was verified."""
    # The link's lock first, the payment's second (the module's docstring), and
    # the link read under its lock: an invoice the cron tied to it a moment ago
    # is the one this payment closes.
    link_id = PaymentLinkPayment.objects.values_list('link_id', flat=True).get(pk=payment_id)
    link = _locked_link(link_id)
    payment = (
        PaymentLinkPayment.objects.select_for_update(of=('self',))
        .select_related('formal_document')
        .get(pk=payment_id)
    )
    payment.link = link
    if payment.formal_document_id:
        return payment.formal_document
    if payment.status != PaymentLinkPayment.STATUS_COMPLETED:
        return None

    # One link, one sale, one document. A second verified payment on it (an
    # attempt retired as abandoned whose notify came late, beside the attempt
    # that replaced it) is money to give back or to document by hand — never a
    # second tax document issued quietly. It stays on the office's list with
    # this reason (document_error).
    documented = (
        link.payments.exclude(pk=payment.pk).filter(formal_document__isnull=False)
        .select_related('formal_document').first()
    )
    if documented is not None:
        raise BusinessChargeDocumentError(
            f'תשלום נוסף על אותה גבייה: לתשלום אחר בקישור הזה כבר הופק {documented.formal_document.document_number}. '
            'לא הופק מסמך נוסף — יש לבדוק מול הלקוח ולזכות את החיוב, או להפיק מסמך ידנית.'
        )
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
        # The line printed on the tax document: the screen's own description
        # often names the invoice already, and it must not read twice.
        line = (
            description if invoice.document_number in description
            else f'תשלום עבור חשבונית עסקה {invoice.document_number} — {description}'
        )
        data = _combined_data(payment, line[:500], settlement=invoice)
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


# ── unpaid after a day: the open tax invoice ────────────────────────────────

def _awaits_overdue_invoice(link: PaymentLink, *, now, not_before) -> bool:
    """Whether the link is still an unpaid business charge that is due its open tax invoice."""
    if link.kind != PaymentLink.KIND_BUSINESS_CHARGE or not link.is_active or link.target_invoice_id:
        return False
    if link.expires_at and link.expires_at <= now:
        return False
    if link.created_at > now - invoice_after():
        return False
    if not_before is not None and link.created_at < not_before:
        return False
    if link.payments.filter(status__in=_FINAL_STATUSES).exists():
        # Paid, or money a person still has to look at: never invoiced beside it.
        return False
    # A payment under way decides which document this sale gets; one started
    # long ago and never finished does not hold the invoice back.
    return not link.payments.filter(
        status=PaymentLinkPayment.STATUS_PENDING, created_at__gt=now - attempt_abandoned_after(),
    ).exists()


def _issue_overdue_invoice(link_id, *, now, not_before):
    """One link, in the caller's transaction: its tax invoice, or None when it is not due one (any more)."""
    link = _locked_link(link_id)
    if not _awaits_overdue_invoice(link, now=now, not_before=not_before):
        return None
    options = list(link.options.filter(is_active=True)[:2])
    if len(options) != 1:
        raise BusinessChargeDocumentError(
            'לקישור גבייה עסקית צריכה להיות אפשרות תשלום פעילה אחת — אי אפשר לדעת על איזה סכום להפיק חשבונית'
        )
    amount = money(options[0].amount)
    validate_business_charge_link(link, amount)
    description = (link.description or link.title or 'שירות')[:500]
    doc = document_service.create_invoice(
        _invoice_data(link, amount, description), 'tax_invoice', issued_by=link.created_by,
    )
    link.target_invoice = doc
    link.save(update_fields=['target_invoice', 'updated_at'])
    return doc


def issue_overdue_business_invoices(*, now=None, limit: int = 20) -> dict:
    """
    The cron: an open tax invoice for every business charge left unpaid for a day.

    A link is due one when it is a business charge, active, not expired, tied
    to no invoice yet, created at least BUSINESS_CHARGE_INVOICE_AFTER_HOURS ago
    (and not before BUSINESS_CHARGE_INVOICE_LINKS_FROM), with no payment
    completed or in review and no attempt still under way. Each link is
    invoiced in a transaction of its own, under its row lock, with every
    condition read again there — so a second run, or a run beside a payment,
    never issues a second document for the sale. The invoice becomes the link's
    `target_invoice`; the payment that comes later closes it with a receipt
    (issue_business_charge_document).

    A link that cannot be invoiced (a document rule refused it) does not stop
    the others: it is logged, returned under 'errors' and tried again on the
    next run. `limit` bounds the invoices issued in one run. Nothing runs while
    BUSINESS_CHARGE_ENABLED is off.

    Returns {'checked': n, 'issued': [...], 'skipped': n, 'errors': [...]}.
    """
    now = now or timezone.now()
    summary = {'checked': 0, 'issued': [], 'skipped': 0, 'errors': []}
    if not getattr(settings, 'BUSINESS_CHARGE_ENABLED', False):
        return {**summary, 'disabled': True}
    try:
        not_before = invoice_links_from()
    except ValueError as exc:
        logger.error('business charge invoices: %s — nothing issued', exc)
        summary['errors'].append({'link_id': '', 'error': str(exc)})
        return summary
    limit = max(1, min(int(limit or 20), 100))

    due = (
        PaymentLink.objects
        .filter(
            kind=PaymentLink.KIND_BUSINESS_CHARGE, is_active=True, target_invoice__isnull=True,
            created_at__lte=now - invoice_after(),
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .exclude(payments__status__in=_FINAL_STATUSES)
        .order_by('created_at')
    )
    if not_before is not None:
        due = due.filter(created_at__gte=not_before)

    for link_id in list(due.values_list('pk', flat=True)[:OVERDUE_SCAN_CAP]):
        if len(summary['issued']) >= limit:
            break
        summary['checked'] += 1
        try:
            with transaction.atomic():
                doc = _issue_overdue_invoice(link_id, now=now, not_before=not_before)
        except Exception as exc:
            logger.exception('business charge link %s: the open tax invoice was not issued', link_id)
            summary['errors'].append({'link_id': str(link_id), 'error': str(exc)[:300]})
            continue
        if doc is None:
            summary['skipped'] += 1
            continue
        logger.info('business charge link %s: unpaid — tax invoice %s issued', link_id, doc.document_number)
        summary['issued'].append({
            'link_id': str(link_id), 'document_number': doc.document_number, 'total': str(doc.total_amount),
        })
    return summary
