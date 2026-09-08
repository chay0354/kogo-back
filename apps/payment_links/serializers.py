from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from apps.core.models import Business, BusinessCategory
from apps.payment_links.models import PaymentLink, PaymentLinkOption, PaymentLinkPayment


class PaymentLinkOptionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)

    class Meta:
        model = PaymentLinkOption
        fields = ['id', 'label', 'amount', 'sort_order', 'is_active']

    def validate_amount(self, value):
        try:
            amount = Decimal(str(value)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError):
            raise serializers.ValidationError('סכום לא תקין')
        if amount < Decimal('1.00'):
            raise serializers.ValidationError('הסכום המינימלי הוא ₪1')
        if amount > Decimal('50000'):
            raise serializers.ValidationError('הסכום גבוה מדי')
        return amount


class PaymentLinkSerializer(serializers.ModelSerializer):
    options = PaymentLinkOptionSerializer(many=True)
    business_name = serializers.CharField(source='business.name', read_only=True, default='')
    business_category_name = serializers.CharField(source='business_category.name', read_only=True, default='')
    branch_name = serializers.CharField(source='branch.name', read_only=True, default='')
    public_url = serializers.SerializerMethodField()
    is_open = serializers.SerializerMethodField()
    paid_count = serializers.IntegerField(read_only=True, default=0)
    paid_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=Decimal('0'))
    review_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PaymentLink
        fields = [
            'id', 'slug', 'title', 'description', 'business', 'business_name',
            'business_category', 'business_category_name', 'branch', 'branch_name',
            'is_active', 'expires_at', 'public_url', 'is_open', 'options',
            'paid_count', 'paid_total', 'review_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']

    def get_public_url(self, obj):
        return obj.public_url()

    def get_is_open(self, obj):
        return obj.is_open()

    def validate(self, data):
        business = data.get('business', getattr(self.instance, 'business', None))
        category = data.get('business_category', getattr(self.instance, 'business_category', None))
        if business is None:
            raise serializers.ValidationError({'business': 'יש לבחור עסק — כל תשלום משויך לעסק'})
        if category is not None and category.business_id != business.id:
            raise serializers.ValidationError({'business_category': 'הקטגוריה אינה שייכת לעסק שנבחר'})
        options = data.get('options')
        if options is not None:
            active = [o for o in options if o.get('is_active', True)]
            if not active:
                raise serializers.ValidationError({'options': 'נדרשת לפחות אפשרות תשלום אחת'})
            if len(options) > 20:
                raise serializers.ValidationError({'options': 'עד 20 אפשרויות'})
        return data

    def _sync_options(self, link, rows):
        # Options are never deleted (payments reference them): an option the
        # office removed is deactivated; a known id is updated; the rest are new.
        existing = {str(o.id): o for o in link.options.all()}
        seen = set()
        for index, row in enumerate(rows):
            row_id = str(row.get('id') or '')
            if row_id and row_id in existing:
                option = existing[row_id]
                option.label = row['label']
                option.amount = row['amount']
                option.sort_order = row.get('sort_order', index)
                option.is_active = row.get('is_active', True)
                option.save(update_fields=['label', 'amount', 'sort_order', 'is_active'])
                seen.add(row_id)
            else:
                option = PaymentLinkOption.objects.create(
                    link=link, label=row['label'], amount=row['amount'],
                    sort_order=row.get('sort_order', index), is_active=row.get('is_active', True),
                )
                seen.add(str(option.id))
        for row_id, option in existing.items():
            if row_id not in seen and option.is_active:
                option.is_active = False
                option.save(update_fields=['is_active'])

    def create(self, validated):
        options = validated.pop('options')
        link = PaymentLink.objects.create(**validated)
        self._sync_options(link, options)
        return link

    def update(self, instance, validated):
        options = validated.pop('options', None)
        for key, value in validated.items():
            setattr(instance, key, value)
        instance.save()
        if options is not None:
            self._sync_options(instance, options)
        return instance


class PaymentLinkPaymentSerializer(serializers.ModelSerializer):
    link_title = serializers.CharField(source='link.title', read_only=True)

    class Meta:
        model = PaymentLinkPayment
        fields = [
            'id', 'link', 'link_title', 'option', 'option_label', 'amount', 'reported_amount',
            'payer_name', 'payer_phone', 'payer_email', 'status', 'gateway_transaction_id',
            'gateway_confirmation_code', 'card_last4', 'card_type', 'failure_reason', 'failure_code',
            'review_reason', 'paid_at', 'formal_document', 'created_at',
        ]
        read_only_fields = [f for f in fields if f != 'formal_document']


class PublicPaymentLinkSerializer(serializers.ModelSerializer):
    """What the payer sees — no tags, no counts, active options only."""
    options = serializers.SerializerMethodField()

    class Meta:
        model = PaymentLink
        fields = ['slug', 'title', 'description', 'options']

    def get_options(self, obj):
        return [
            {'id': str(o.id), 'label': o.label, 'amount': str(o.amount)}
            for o in obj.options.all() if o.is_active
        ]
