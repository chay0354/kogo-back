"""Where payment-link money shows up: the business dashboard and the undocumented-income report."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Q

from apps.payment_links.models import PaymentLinkPayment


def completed_link_payments(date_from: date, date_to: date, *, branch_id=None, branch_ids=None):
    qs = (
        PaymentLinkPayment.objects
        .filter(status=PaymentLinkPayment.STATUS_COMPLETED, paid_at__date__gte=date_from, paid_at__date__lte=date_to)
        # A payment the office already issued a business document for is counted
        # through that document; counting it here too would double it.
        .filter(formal_document__isnull=True)
        .select_related('link', 'link__business', 'link__business_category', 'link__branch')
    )
    if branch_id and branch_id != 'all':
        qs = qs.filter(link__branch_id=branch_id)
    elif branch_ids is not None:
        qs = qs.filter(link__branch_id__in=branch_ids)
    return qs


def aggregate_payment_link_revenue(date_from: date, date_to: date, branch_id=None, branch_ids=None) -> list:
    """Rows in the shape revenue_service._combine_income reads (`document_rows`)."""
    rows = []
    for row in completed_link_payments(date_from, date_to, branch_id=branch_id, branch_ids=branch_ids):
        link = row.link
        rows.append({
            'business_id': str(link.business_id) if link.business_id else '',
            'business_name': link.business.name if link.business_id else '',
            'category_id': str(link.business_category_id) if link.business_category_id else '',
            'category_name': link.business_category.name if link.business_category_id else '',
            'amount': Decimal(row.amount),
        })
    return rows


def card_link_one_time_rows(date_from: date, date_to: date, branch_id=None, branch_ids=None) -> list:
    """
    One-time charges taken through a card link, tagged by the link's business
    and category. Such a Payment has no lesson, so the lesson aggregation never
    sees it; without this it would be missing from the dashboard.
    """
    from apps.customers.models import Payment

    qs = (
        Payment.objects
        .filter(
            payment_type='one_time', status='completed', lesson__isnull=True,
            card_link__isnull=False, payment_date__date__gte=date_from, payment_date__date__lte=date_to,
        )
        .select_related('card_link', 'card_link__business', 'card_link__business_category')
    )
    if branch_id and branch_id != 'all':
        qs = qs.filter(branch_id=branch_id)
    elif branch_ids is not None:
        qs = qs.filter(branch_id__in=branch_ids)
    rows = []
    for payment in qs:
        link = payment.card_link
        rows.append({
            'business_id': str(link.business_id) if link.business_id else '',
            'business_name': link.business.name if link.business_id else '',
            'category_id': str(link.business_category_id) if link.business_category_id else '',
            'category_name': link.business_category.name if link.business_category_id else '',
            'amount': Decimal(payment.final_amount),
        })
    return rows
