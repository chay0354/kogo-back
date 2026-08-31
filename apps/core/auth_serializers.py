from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from apps.core.models import Branch, UserProfile


User = get_user_model()


class LoginSerializer(serializers.Serializer):
    email = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_str
        from django.utils.http import urlsafe_base64_decode

        try:
            uid = force_str(urlsafe_base64_decode(attrs['uid']))
            user = User.objects.get(pk=uid, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({'token': 'קישור איפוס לא תקין או שפג תוקפו'})

        if not default_token_generator.check_token(user, attrs['token']):
            raise serializers.ValidationError({'token': 'קישור איפוס לא תקין או שפג תוקפו'})

        validate_password(attrs['password'], user=user)
        attrs['user'] = user
        return attrs

    def save(self):
        from rest_framework.authtoken.models import Token

        user = self.validated_data['user']
        user.set_password(self.validated_data['password'])
        user.save(update_fields=['password'])
        Token.objects.filter(user=user).delete()
        return user


class CurrentUserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    branch_ids = serializers.SerializerMethodField()
    login_count = serializers.SerializerMethodField()
    tour_completed = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'is_active', 'is_superuser',
            'role', 'branch_ids', 'login_count', 'tour_completed',
        ]

    def get_login_count(self, obj):
        try:
            return obj.profile.login_count
        except UserProfile.DoesNotExist:
            return 0

    def get_tour_completed(self, obj):
        try:
            return obj.profile.tour_completed_at is not None
        except UserProfile.DoesNotExist:
            return False

    def get_role(self, obj):
        try:
            return obj.profile.role
        except UserProfile.DoesNotExist:
            return None

    def get_branch_ids(self, obj):
        try:
            if obj.profile.role != UserProfile.ROLE_PARTNER:
                return []
            return [str(bid) for bid in obj.profile.assigned_branches.values_list('id', flat=True)]
        except UserProfile.DoesNotExist:
            return []


class ManagedUserSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=UserProfile.ROLE_CHOICES, write_only=True, required=False)
    branch_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        trim_whitespace=False,
        help_text="Set to change password",
    )
    # Read-only mirrors
    role_display = serializers.SerializerMethodField(read_only=True)
    email = serializers.CharField()

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
            'branch_ids',
            'password',
        ]

    def get_role_display(self, obj):
        try:
            return obj.profile.role
        except UserProfile.DoesNotExist:
            return None

    def validate_email(self, value):
        login_id = (value or '').strip()
        if not login_id:
            raise serializers.ValidationError('שם משתמש נדרש')
        qs = User.objects.filter(Q(email__iexact=login_id) | Q(username__iexact=login_id))
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('שם משתמש כבר קיים במערכת')
        return login_id

    def validate(self, attrs):
        password = attrs.get('password')
        if password:
            validate_password(password)
        role = attrs.get('role')
        branch_ids = attrs.get('branch_ids')
        if role == UserProfile.ROLE_PARTNER or (
            self.instance
            and getattr(self.instance, 'profile', None)
            and self.instance.profile.role == UserProfile.ROLE_PARTNER
            and role is None
        ):
            effective_role = role or (self.instance.profile.role if self.instance else None)
            if effective_role == UserProfile.ROLE_PARTNER and branch_ids is not None and len(branch_ids) == 0:
                raise serializers.ValidationError({'branch_ids': 'יש לבחור לפחות סניף אחד'})
        if branch_ids:
            existing = set(Branch.objects.filter(id__in=branch_ids, is_active=True).values_list('id', flat=True))
            missing = [str(v) for v in branch_ids if v not in existing]
            if missing:
                raise serializers.ValidationError({'branch_ids': 'סניף לא תקין או לא פעיל'})
        return attrs

    def _sync_partner_branches(self, profile, branch_ids):
        if branch_ids is None:
            return
        profile.assigned_branches.set(branch_ids)

    @transaction.atomic
    def create(self, validated_data):
        role = validated_data.pop('role', None)
        branch_ids = validated_data.pop('branch_ids', None)
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
        if role == UserProfile.ROLE_PARTNER:
            self._sync_partner_branches(profile, branch_ids or [])
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        role = validated_data.pop('role', None)
        branch_ids = validated_data.pop('branch_ids', None)
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
            if role == UserProfile.ROLE_PARTNER:
                self._sync_partner_branches(profile, branch_ids or [])
            elif role != UserProfile.ROLE_PARTNER:
                profile.assigned_branches.clear()
        elif branch_ids is not None:
            try:
                if instance.profile.role == UserProfile.ROLE_PARTNER:
                    self._sync_partner_branches(instance.profile, branch_ids)
            except UserProfile.DoesNotExist:
                pass

        return instance


