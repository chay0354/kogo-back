from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from apps.core.models import Branch, UserProfile


User = get_user_model()


class PartnerSerializer(serializers.ModelSerializer):
    branch_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )
    branches = serializers.SerializerMethodField(read_only=True)
    full_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'first_name',
            'last_name',
            'full_name',
            'is_active',
            'branches',
            'branch_ids',
            'password',
        ]
        read_only_fields = ['id', 'full_name', 'branches']
        extra_kwargs = {
            'password': {'write_only': True, 'required': False},
        }

    def get_full_name(self, obj):
        return f'{obj.first_name or ""} {obj.last_name or ""}'.strip() or obj.email

    def get_branches(self, obj):
        try:
            profile = obj.profile
        except UserProfile.DoesNotExist:
            return []
        return [
            {'id': str(branch.id), 'name': branch.name}
            for branch in profile.assigned_branches.all().order_by('name')
        ]

    def validate_email(self, value):
        email = value.strip().lower()
        qs = User.objects.filter(email__iexact=email)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('אימייל כבר קיים במערכת')
        return email

    def validate_branch_ids(self, value):
        if not value:
            return []
        existing = set(
            Branch.objects.filter(id__in=value, is_active=True).values_list('id', flat=True)
        )
        missing = [str(v) for v in value if v not in existing]
        if missing:
            raise serializers.ValidationError('סניף לא תקין או לא פעיל')
        return value

    def validate(self, attrs):
        password = attrs.get('password')
        if password:
            try:
                validate_password(password)
            except DjangoValidationError as exc:
                # Surface under the password field so the UI shows it next to the input
                raise serializers.ValidationError({'password': list(exc.messages)})
        if not self.instance and not password:
            raise serializers.ValidationError({'password': 'סיסמה נדרשת'})
        branch_ids = attrs.get('branch_ids')
        if branch_ids is not None and len(branch_ids) == 0:
            raise serializers.ValidationError({'branch_ids': 'יש לבחור לפחות סניף אחד'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        branch_ids = validated_data.pop('branch_ids', [])
        password = validated_data.pop('password')
        email = validated_data['email']

        user = User(
            username=email,
            email=email,
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            is_active=validated_data.get('is_active', True),
        )
        user.set_password(password)
        user.save()

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_PARTNER
        profile.save()
        profile.assigned_branches.set(branch_ids)
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        branch_ids = validated_data.pop('branch_ids', None)
        password = validated_data.pop('password', None)

        if 'email' in validated_data:
            email = validated_data['email']
            instance.email = email
            instance.username = email
        for field in ['first_name', 'last_name', 'is_active']:
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        if password:
            instance.set_password(password)
        instance.save()

        profile, _ = UserProfile.objects.get_or_create(user=instance)
        profile.role = UserProfile.ROLE_PARTNER
        profile.save()
        if branch_ids is not None:
            profile.assigned_branches.set(branch_ids)
        return instance
