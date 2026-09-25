"""API shapes for standing orders, their charges and card links. Read only: writes go through orders.py and billing.py.

Money on a standing order is in shekels, as strings ('1234.56'), like the
tenancy. Money on a charge is stored in agorot and given both ways:
`total_agorot` (an integer) and `total` (shekels, a string).
The card token is never in any response.
"""
from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers

from apps.core.frontend_url import public_frontend_url
from apps.rental_billing import billing
from apps.rental_billing.billing import UNDECIDED_STATUSES, is_undecided, shekels, split_amount
from apps.rental_billing.links import public_url
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.offline import late_card_charge, offline_payment_of
from apps.rental_billing.receipts import OFFLINE_METHOD_LABELS


def _name(user) -> str:
    if not user:
        return ''
    return (user.get_full_name() or user.username or '').strip()


# The chip of a month paid at the office. Its status is 'charged' — every reader
# takes that for "paid, never charge again" — and this says how it was paid.
OFFLINE_STATUS_LABELS = {
    'cash': 'שולם במזומן',
    'check': "שולם בצ'ק",
    'bank_transfer': 'שולם בהעברה',
}


def signed_originals_by_number(charges) -> dict:
    """
    The signed originals of these charges' receipts, by number — one query for
    a whole list, and never the PDF bytes. Empty while signing is off (no rows).
    """
    from apps.documents.models import SignedOriginal

    numbers = [charge.receipt.document_number for charge in charges if charge.receipt_id]
    if not numbers:
        return {}
    rows = SignedOriginal.objects.filter(number__in=numbers).only(
        'id', 'number', 'purpose', 'delivery', 'delivery_reason', 'sent_at', 'email_to', 'signed_at',
        'paper_original_printed_at',
    )
    return {row.number: row for row in rows}


def _delivery_payload(row) -> dict | None:
    """Where the receipt's signed original went: by mail, on paper (and whether it was handed over), or why it waits."""
    if row is None or row.is_archive_copy:
        return None
    return {
        'delivery': row.delivery,
        'label': row.get_delivery_display(),
        'reason': row.delivery_reason,
        'signed': row.signed_at is not None,
        'sent_at': row.sent_at.isoformat() if row.sent_at else None,
        'paper_printed_at': row.paper_original_printed_at.isoformat() if row.paper_original_printed_at else None,
    }


def card_link_payload(link: TenantCardLink | None, request=None) -> dict | None:
    if link is None:
        return None
    live = link.status in TenantCardLink.LIVE_STATUSES
    return {
        'id': str(link.id),
        'status': link.status,
        'status_label': link.get_status_display(),
        'url': public_url(link, public_frontend_url(request)) if live else '',
        # A card link no longer closes with time. Both stay in the shape — always
        # False and null — so the office's screens keep reading the same fields.
        'expired': False,
        'expires_at': None,
        'attempts': link.attempts,
        'last_error': link.last_error,
        'review_reason': link.review_reason,
        'used_at': link.used_at.isoformat() if link.used_at else None,
        'created_at': link.created_at.isoformat(),
    }


class StandingOrderSerializer(serializers.ModelSerializer):
    tenancy_id = serializers.UUIDField(read_only=True)
    tenant = serializers.SerializerMethodField()
    branch_id = serializers.UUIDField(read_only=True, allow_null=True)
    branch_name = serializers.SerializerMethodField()
    business_id = serializers.UUIDField(read_only=True, allow_null=True)
    business_name = serializers.SerializerMethodField()
    business_category_id = serializers.UUIDField(read_only=True, allow_null=True)
    business_category_name = serializers.SerializerMethodField()
    amount_before_vat = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    vat_amount = serializers.SerializerMethodField()
    monthly_total = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    source_label = serializers.CharField(source='get_source_display', read_only=True)
    has_card = serializers.BooleanField(read_only=True)
    card_expiry = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    card_link = serializers.SerializerMethodField()
    blocked_by_charge = serializers.SerializerMethodField()
    months_never_charged = serializers.SerializerMethodField()

    class Meta:
        model = TenantStandingOrder
        fields = [
            'id', 'tenancy_id', 'tenant', 'branch_id', 'branch_name',
            'business_id', 'business_name', 'business_category_id', 'business_category_name',
            'amount_before_vat', 'vat_amount', 'monthly_total', 'billing_day', 'start_date', 'end_date',
            'next_charge_date', 'status', 'status_label', 'source', 'source_label',
            'has_card', 'card_last4', 'card_expiry', 'last_error', 'failed_at', 'notes',
            'created_by_name', 'created_at', 'updated_at', 'card_link',
            'blocked_by_charge', 'months_never_charged',
        ]
        read_only_fields = fields

    def get_tenant(self, obj) -> dict:
        tenant = obj.tenant
        return {
            'id': str(tenant.id), 'full_name': tenant.full_name, 'company_number': tenant.company_number,
            'id_number': tenant.id_number, 'phone': tenant.phone, 'email': tenant.email,
        }

    def get_branch_name(self, obj) -> str:
        return obj.branch.name if obj.branch_id else ''

    def get_business_name(self, obj) -> str:
        return obj.business.name if obj.business_id else ''

    def get_business_category_name(self, obj) -> str:
        return obj.business_category.name if obj.business_category_id else ''

    def get_vat_amount(self, obj) -> str:
        _net, vat, _total = split_amount(obj.amount_before_vat)
        return str(shekels(vat))

    def get_monthly_total(self, obj) -> str:
        return str(shekels(split_amount(obj.amount_before_vat)[2]))

    def get_card_expiry(self, obj) -> str:
        if not obj.card_expire_month or not obj.card_expire_year:
            return ''
        return f'{obj.card_expire_month:02d}/{obj.card_expire_year % 100:02d}'

    def get_created_by_name(self, obj) -> str:
        return _name(obj.created_by)

    def _tenancy_charges(self, obj):
        """The tenancy's charges, prefetched by the view (never a query per order)."""
        return getattr(getattr(obj, 'tenancy', None), 'all_charges', None)

    def get_blocked_by_charge(self, obj):
        """
        The month holding this order up: while one of its tenancy's months is
        reserved or in review, nothing on that tenancy is charged. None when
        there is none. The office screen badges it.
        """
        charges = self._tenancy_charges(obj)
        if charges is None:
            charges = obj.tenancy.rental_charges.all()
        undecided = sorted(
            (charge for charge in charges if charge.status in UNDECIDED_STATUSES), key=lambda c: c.period,
        )
        if not undecided:
            return None
        charge = undecided[0]
        return {
            'id': str(charge.id),
            'period': charge.period.isoformat(),
            'status': charge.status,
            'status_label': charge.get_status_display(),
        }

    def get_months_never_charged(self, obj) -> list:
        """
        Months of this order, before the current one, that carry no charge at
        all — skipped by a run that found them too old, or passed while it was
        paused or waiting for a card. Never charged by themselves; the office
        decides what to do with them.
        """
        charges = self._tenancy_charges(obj)
        if charges is None:
            charges = list(obj.tenancy.rental_charges.all())
        return [
            month.isoformat()
            for month in billing.months_never_charged(obj, billing.today_local(), charges=charges)
        ]

    def get_card_link(self, obj):
        # The newest link, whatever its state; a URL only while it can still be used.
        links = getattr(obj, 'recent_card_links', None)
        link = links[0] if links else (None if links is not None else obj.card_links.order_by('-created_at').first())
        return card_link_payload(link, self.context.get('request'))


class TenantChargeSerializer(serializers.ModelSerializer):
    standing_order_id = serializers.UUIDField(read_only=True)
    tenancy_id = serializers.SerializerMethodField()
    tenant_name = serializers.SerializerMethodField()
    branch_id = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()
    trigger_label = serializers.CharField(source='get_trigger_display', read_only=True)
    amount_before_vat_agorot = serializers.IntegerField(source='amount_before_vat', read_only=True)
    vat_amount_agorot = serializers.IntegerField(source='vat_amount', read_only=True)
    total_agorot = serializers.IntegerField(source='total', read_only=True)
    amount_before_vat = serializers.SerializerMethodField()
    vat_amount = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    business_id = serializers.UUIDField(read_only=True)
    business_name = serializers.SerializerMethodField()
    business_category_id = serializers.UUIDField(read_only=True, allow_null=True)
    business_category_name = serializers.SerializerMethodField()
    receipt = serializers.SerializerMethodField()
    needs_receipt = serializers.SerializerMethodField()
    undecided = serializers.SerializerMethodField()
    resolved_by_name = serializers.SerializerMethodField()
    offline_payment = serializers.SerializerMethodField()
    late_card_charge = serializers.SerializerMethodField()

    class Meta:
        model = TenantCharge
        fields = [
            'id', 'standing_order_id', 'tenancy_id', 'tenant_name', 'branch_id', 'branch_name', 'period',
            'status', 'status_label', 'trigger', 'trigger_label', 'attempts',
            'amount_before_vat_agorot', 'vat_amount_agorot', 'total_agorot',
            'amount_before_vat', 'vat_amount', 'total',
            'business_id', 'business_name', 'business_category_id', 'business_category_name',
            'card_last4', 'transaction_id', 'confirmation_code', 'response_code', 'error',
            'reserved_at', 'charged_at', 'receipt', 'receipt_error', 'receipt_emailed_at', 'needs_receipt',
            'undecided', 'resolved_by_name', 'resolved_at', 'resolution_note', 'created_at',
            'offline_payment', 'late_card_charge',
        ]
        read_only_fields = fields

    def get_tenancy_id(self, obj) -> str:
        return str(obj.tenancy_id)

    def get_status_label(self, obj) -> str:
        payment = offline_payment_of(obj) if obj.status == TenantCharge.STATUS_CHARGED else None
        if payment is not None:
            return OFFLINE_STATUS_LABELS.get(payment.payment_method, obj.get_status_display())
        return obj.get_status_display()

    def get_offline_payment(self, obj):
        """How a month paid at the office was paid, as its receipt names it; None for a card, or no payment."""
        payment = offline_payment_of(obj)
        if payment is None:
            return None
        is_check = payment.payment_method == 'check'
        return {
            'method': payment.payment_method,
            'method_label': OFFLINE_METHOD_LABELS.get(payment.payment_method, payment.payment_method),
            'amount': str(payment.amount),
            'paid_on': payment.paid_on.isoformat() if payment.paid_on else None,
            'reference': '' if is_check else payment.reference,
            'check_number': payment.reference if is_check else '',
            'check_bank': payment.check_bank,
            'check_branch': payment.check_branch,
            'check_account': payment.check_account,
            'check_date': payment.check_date.isoformat() if payment.check_date else None,
            'check_crossed': bool(payment.check_crossed),
        }

    def get_late_card_charge(self, obj) -> bool:
        """Tranzila charged the card on a month already voided or paid at the office: the office must decide."""
        return late_card_charge(obj)

    def get_tenant_name(self, obj) -> str:
        return obj.standing_order.tenant.full_name

    def get_branch_id(self, obj):
        return str(obj.standing_order.branch_id) if obj.standing_order.branch_id else None

    def get_branch_name(self, obj) -> str:
        return obj.standing_order.branch.name if obj.standing_order.branch_id else ''

    def get_amount_before_vat(self, obj) -> str:
        return str(shekels(obj.amount_before_vat))

    def get_vat_amount(self, obj) -> str:
        return str(shekels(obj.vat_amount))

    def get_total(self, obj) -> str:
        return str(shekels(obj.total))

    def get_business_name(self, obj) -> str:
        return obj.business.name

    def get_business_category_name(self, obj) -> str:
        return obj.business_category.name if obj.business_category_id else ''

    def get_receipt(self, obj):
        if not obj.receipt_id:
            return None
        doc = obj.receipt
        charged_on = timezone.localdate(obj.charged_at) if obj.charged_at else doc.document_date
        return {
            'id': str(doc.id),
            'document_number': doc.document_number,
            'document_date': doc.document_date.isoformat(),
            # Issued on a later day than the charge (marked "הופק באיחור" on the receipt).
            'issued_late': doc.document_date != charged_on,
            # The documents module serves the PDF; the screen downloads it with its own credentials.
            'pdf_url': reverse('document-pdf', args=[doc.pk]),
            # Where its signed original went (by mail, on paper, held and why);
            # None while signing is off. receipt_emailed_at says when the mail left.
            'delivery': _delivery_payload(self._signed_original(doc.document_number)),
        }

    def _signed_original(self, number: str):
        """From the view's one query for the list when it made one, else looked up for this receipt alone."""
        originals = self.context.get('signed_originals')
        if originals is not None:
            return originals.get(number)
        from apps.documents.models import SignedOriginal

        return SignedOriginal.objects.filter(number=number).defer('pdf').first()

    def get_needs_receipt(self, obj) -> bool:
        """Charged, and no receipt: the office issues it again from here."""
        return obj.status == TenantCharge.STATUS_CHARGED and not obj.receipt_id

    def get_undecided(self, obj) -> bool:
        """In review, or a reservation that never heard back: waiting for the office's decision."""
        return is_undecided(obj)

    def get_resolved_by_name(self, obj) -> str:
        return _name(obj.resolved_by)
