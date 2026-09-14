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
from apps.rental_billing.links import expires_at, is_expired, public_url
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder


def _name(user) -> str:
    if not user:
        return ''
    return (user.get_full_name() or user.username or '').strip()


def card_link_payload(link: TenantCardLink | None, request=None) -> dict | None:
    if link is None:
        return None
    live = link.status in TenantCardLink.LIVE_STATUSES and not is_expired(link)
    return {
        'id': str(link.id),
        'status': link.status,
        'status_label': link.get_status_display(),
        'url': public_url(link, public_frontend_url(request)) if live else '',
        'expired': is_expired(link),
        'expires_at': expires_at(link).isoformat(),
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
    status_label = serializers.CharField(source='get_status_display', read_only=True)
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
        ]
        read_only_fields = fields

    def get_tenancy_id(self, obj) -> str:
        return str(obj.tenancy_id)

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
        }

    def get_needs_receipt(self, obj) -> bool:
        """Charged, and no receipt: the office issues it again from here."""
        return obj.status == TenantCharge.STATUS_CHARGED and not obj.receipt_id

    def get_undecided(self, obj) -> bool:
        """In review, or a reservation that never heard back: waiting for the office's decision."""
        return is_undecided(obj)

    def get_resolved_by_name(self, obj) -> str:
        return _name(obj.resolved_by)
