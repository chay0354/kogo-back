"""API shapes for tenancies and their contracts: what the office reads, and what it may write.

A tenancy is read with its tenant and its slots nested, and written with either
`tenant_id` (a merchant already on file) or `tenant` (the details of a new one,
or on PATCH the changes to the one it has). Branch scoping is enforced here, on
write, the way the rest of the app scopes a write: a partner may only put a
tenancy in one of their own branches, and may only attach a merchant they can see.

A tenancy also shows its current contract, the newest one that is not void,
and whether the tenancy changed after it was issued (is_stale). Contracts are
read only here: they are issued and voided by their own endpoints
(apps/rentals/contracts.py) and never edited.
"""
from __future__ import annotations

from django.db import transaction
from django.urls import reverse
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.core.scoping import is_scoped_partner, partner_branch_ids, scope_business_customers
from apps.customers.models import BusinessCustomer
from apps.rentals.contracts import contract_is_stale, current_contract
from apps.rentals.models import BILLING_DAY_MAX, BILLING_DAY_MIN, RentalContract, Tenancy
from apps.rentals.slots import suggested_monthly_amount
from apps.rentals.tenants import create_tenant, update_tenant
from apps.scheduling.models import ScheduleEvent


def _request_user(serializer):
    request = serializer.context.get('request')
    return getattr(request, 'user', None)


def check_partner_branch(user, branch) -> None:
    """
    A scoped partner may place a tenancy only in one of their own branches.

    Fails closed: no branch at all, or a partner with no branches assigned, is
    refused as well. A tenancy outside their branches is one they could never
    open again.
    """
    if not is_scoped_partner(user):
        return
    if branch is None:
        raise PermissionDenied('יש לבחור אחד מהסניפים שלך')
    if branch.pk not in set(partner_branch_ids(user)):
        raise PermissionDenied('אין הרשאה לסניף הזה')


class TenantSerializer(serializers.ModelSerializer):
    """A tenant as the tenancy shows it, and the fields the tenancy screen may write."""

    full_name = serializers.ReadOnlyField()

    class Meta:
        model = BusinessCustomer
        fields = [
            'id', 'first_name', 'last_name', 'full_name',
            'company_number', 'id_number', 'phone', 'email', 'address',
        ]
        read_only_fields = ['id', 'full_name']
        # A company tenant has one name — the tenants screen writes it as
        # "שם העסק" in last_name — and a person has two. Either field may be
        # empty, never both (validate).
        extra_kwargs = {
            'first_name': {'required': False, 'allow_blank': True},
            'last_name': {'required': False, 'allow_blank': True},
        }

    def validate(self, attrs):
        # A new tenant needs a name. An edit that sends both names must leave one;
        # an edit that sends one keeps the other as it is on the card.
        partial = getattr(self.root, 'partial', False)
        both_sent = 'first_name' in attrs and 'last_name' in attrs
        names = (attrs.get('first_name', ''), attrs.get('last_name', ''))
        if (not partial or both_sent) and not any(str(name).strip() for name in names):
            raise serializers.ValidationError({'last_name': 'יש להזין את שם העסק, או שם פרטי ושם משפחה'})
        return attrs


class TenancySlotSerializer(serializers.ModelSerializer):
    """A studio-rental event as it appears inside a tenancy or a suggestion. Read only."""

    studio_name = serializers.CharField(source='studio.name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)

    class Meta:
        model = ScheduleEvent
        fields = [
            'id', 'name', 'studio_name', 'branch_name', 'event_type', 'event_date',
            'weekly_repeat_days', 'weekly_day_times', 'start_time', 'end_time',
            'price_per_session', 'is_active', 'contract_start_date', 'contract_end_date',
        ]


class RentalContractSerializer(serializers.ModelSerializer):
    """One issued contract, as the tenancy's contracts list shows it. Read only."""

    status_label = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = RentalContract
        fields = [
            'id', 'version', 'status', 'status_label', 'created_at', 'created_by_name',
            'voided_at', 'void_reason', 'terms_sha256', 'pdf_url',
        ]
        read_only_fields = fields

    def get_created_by_name(self, obj) -> str:
        user = obj.created_by
        if not user:
            return ''
        return (user.get_full_name() or user.username or '').strip()

    def get_pdf_url(self, obj) -> str:
        # The API path of the stored PDF; the screen downloads it with its own credentials.
        return reverse('rental-contract-pdf', args=[obj.pk])


class CurrentContractSerializer(serializers.ModelSerializer):
    """The tenancy's current contract in brief. is_stale is added by the tenancy, which knows its own terms."""

    status_label = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = RentalContract
        fields = ['id', 'version', 'status', 'status_label', 'created_at']
        read_only_fields = fields


class TenancySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    # Declared rather than generated so the range messages are the office's own.
    monthly_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    billing_day = serializers.IntegerField(required=False)
    monthly_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    suggested_monthly_amount = serializers.SerializerMethodField()
    tenant = TenantSerializer(required=False)
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=BusinessCustomer.objects.all(), write_only=True, required=False,
    )
    slots = TenancySlotSerializer(many=True, read_only=True)
    current_contract = serializers.SerializerMethodField()

    class Meta:
        model = Tenancy
        fields = [
            'id', 'status', 'status_label', 'branch', 'branch_name',
            'monthly_amount', 'monthly_total', 'billing_day', 'start_date', 'end_date',
            'notes', 'created_at', 'suggested_monthly_amount',
            'tenant', 'tenant_id', 'slots', 'current_contract',
        ]
        read_only_fields = ['id', 'created_at']

    def get_fields(self):
        fields = super().get_fields()
        user = _request_user(self)
        if user is not None:
            # A partner attaches only a merchant they can see: the same scope
            # the business-customer list gives them — their branches' and the
            # ones with no branch. Anyone else is not found. Editing that
            # merchant's card through the tenancy stays narrower (validate).
            fields['tenant_id'].queryset = scope_business_customers(BusinessCustomer.objects.all(), user)
        return fields

    def get_suggested_monthly_amount(self, obj) -> str:
        # A string, like every other amount in this API.
        return f'{suggested_monthly_amount(obj.slots.all()):.2f}'

    def get_current_contract(self, obj):
        contract = current_contract(obj)
        if contract is None:
            return None
        data = CurrentContractSerializer(contract).data
        # The agreement changed after the contract was issued: the terms the
        # tenancy gives now fingerprint differently from the contract's own.
        data['is_stale'] = contract_is_stale(obj, contract)
        return data

    def validate_monthly_amount(self, value):
        if value < 0:
            raise serializers.ValidationError('הסכום החודשי לא יכול להיות שלילי')
        return value

    def validate_billing_day(self, value):
        if not BILLING_DAY_MIN <= value <= BILLING_DAY_MAX:
            raise serializers.ValidationError(
                f'יום החיוב חייב להיות בין {BILLING_DAY_MIN} ל־{BILLING_DAY_MAX}'
            )
        return value

    def validate(self, attrs):
        inst = self.instance
        user = _request_user(self)
        if 'tenant' in attrs and 'tenant_id' in attrs:
            raise serializers.ValidationError({'tenant': 'יש לבחור שוכר קיים או להזין שוכר חדש — לא את שניהם'})
        if inst is None and 'tenant' not in attrs and 'tenant_id' not in attrs:
            raise serializers.ValidationError({'tenant': 'יש לבחור שוכר קיים או להזין פרטי שוכר חדש'})

        branch = attrs['branch'] if 'branch' in attrs else (inst.branch if inst else None)
        check_partner_branch(user, branch)

        if inst is not None and getattr(branch, 'pk', None) != inst.branch_id and inst.slots.exists():
            # A slot joins only a tenancy of its own branch. Moving the tenancy
            # would leave its slots behind in the old one.
            raise serializers.ValidationError({'branch': 'יש לנתק את השכירויות מההסכם לפני העברתו לסניף אחר'})

        if inst is not None and 'tenant' in attrs and is_scoped_partner(user):
            # Editing the tenant edits the merchant's card, which other
            # documents share. A partner edits only a card in their branches.
            if inst.tenant.branch_id not in set(partner_branch_ids(user)):
                raise PermissionDenied('אין הרשאה לערוך את פרטי השוכר הזה')

        start = attrs.get('start_date', inst.start_date if inst else None)
        end = attrs.get('end_date', inst.end_date if inst else None)
        if start and end and end < start:
            raise serializers.ValidationError({'end_date': 'תאריך הסיום לא יכול להיות לפני תאריך ההתחלה'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        tenant_data = validated_data.pop('tenant', None)
        tenant = validated_data.pop('tenant_id', None)
        if tenant is None:
            tenant = create_tenant(tenant_data, validated_data.get('branch'))
        return Tenancy.objects.create(tenant=tenant, **validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        tenant_data = validated_data.pop('tenant', None)
        tenant = validated_data.pop('tenant_id', None)
        if tenant is not None:
            instance.tenant = tenant
        elif tenant_data is not None:
            update_tenant(instance.tenant, tenant_data)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class SuggestionSerializer(serializers.Serializer):
    """One group from slots.rental_suggestions. Read only."""

    key = serializers.CharField()
    renter_name = serializers.CharField()
    renter_id_number = serializers.CharField()
    branch = serializers.UUIDField()
    branch_name = serializers.CharField()
    slots = TenancySlotSerializer(many=True)
    suggested_monthly_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    contract_start_date = serializers.DateField(allow_null=True)
    contract_end_date = serializers.DateField(allow_null=True)
    existing_tenant = serializers.DictField(allow_null=True)
