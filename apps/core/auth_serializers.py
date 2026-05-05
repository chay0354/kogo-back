from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from apps.core.models import UserProfile


User = get_user_model()


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class CurrentUserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'is_active', 'role']

    def get_role(self, obj):
        try:
            return obj.profile.role
        except UserProfile.DoesNotExist:
            return None


class ManagedUserSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=UserProfile.ROLE_CHOICES, write_only=True, required=False)
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        trim_whitespace=False,
        help_text="Set to change password",
    )
    # Read-only mirrors
    role_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'first_name',
            'last_name',
            'is_active',
            'role',
            'role_display',
            'password',
        ]

    def get_role_display(self, obj):
        try:
            return obj.profile.role
        except UserProfile.DoesNotExist:
            return None

    def validate_email(self, value):
        email = value.strip().lower()
        qs = User.objects.filter(email__iexact=email)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('אימייל כבר קיים במערכת')
        return email

    def validate(self, attrs):
        password = attrs.get('password')
        if password:
            validate_password(password)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        role = validated_data.pop('role', None)
        password = validated_data.pop('password', None)

        email = validated_data.get('email')
        # Use email as username for uniqueness and compatibility with Django's default User
        user = User(
            username=email,
            email=email,
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            is_active=validated_data.get('is_active', True),
        )
        if not password:
            raise serializers.ValidationError({'password': 'סיסמה נדרשת'})
        if not role:
            raise serializers.ValidationError({'role': 'תפקיד נדרש'})
        user.set_password(password)
        user.save()

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = role
        profile.save()
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        role = validated_data.pop('role', None)
        password = validated_data.pop('password', None)

        # Prevent self-disable (basic safety)
        request = self.context.get('request')
        if request and request.user and request.user.pk == instance.pk:
            if 'is_active' in validated_data and validated_data['is_active'] is False:
                raise serializers.ValidationError({'is_active': 'לא ניתן להשבית את המשתמש הנוכחי'})
            if role and role != UserProfile.ROLE_MANAGER:
                raise serializers.ValidationError({'role': 'לא ניתן לשנות את התפקיד של המשתמש הנוכחי מתפקיד מנהל'})

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

        if role:
            profile, _ = UserProfile.objects.get_or_create(user=instance)
            profile.role = role
            profile.save()

        return instance


