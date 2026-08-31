from datetime import timedelta

import logging

from django.conf import settings
from django.contrib.auth import authenticate
from django.db.models import F
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.auth import get_user_model
from apps.core.models import UserProfile
from apps.core.auth_serializers import (
    LoginSerializer,
    CurrentUserSerializer,
    ManagedUserSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
)
from apps.core.permissions import IsManager


User = get_user_model()
logger = logging.getLogger(__name__)


class LoginView(APIView):
    """
    Do not run Token/cookie auth here: a stale token in Authorization or auth_token
    cookie would raise 'Invalid token.' before email/password is checked.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        user = authenticate(request, email=email, password=password)
        if not user:
            return Response({'error': 'שם משתמש או סיסמה שגויים'}, status=status.HTTP_401_UNAUTHORIZED)

        # Create or reuse token
        token, _ = Token.objects.get_or_create(user=user)

        # Count this sign-in. F() so two tabs signing in at once cannot both
        # read the same value and write the same increment.
        profile = getattr(user, 'profile', None)
        if profile is not None:
            UserProfile.objects.filter(pk=profile.pk).update(login_count=F('login_count') + 1)
            profile.refresh_from_db(fields=['login_count'])

        response = Response(
            {
                'user': CurrentUserSerializer(user).data,
                # Lets SPA on another origin authenticate (SameSite=Lax cookies are not sent on cross-site XHR).
                'token': token.key,
            },
            status=status.HTTP_200_OK,
        )

        # Cookie-based token for the frontend (credentials: include)
        max_age = int(timedelta(days=30).total_seconds())
        response.set_cookie(
            key='auth_token',
            value=token.key,
            max_age=max_age,
            httponly=True,
            secure=not settings.DEBUG,
            samesite='Lax',
            path='/',
        )

        return response


class ForgotPasswordView(APIView):
    """
    Request a password-reset email. Always returns success to avoid email enumeration.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email'].strip().lower()

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            try:
                from apps.core.password_reset_email import send_password_reset_email
                send_password_reset_email(user)
            except Exception:
                logger.exception('Password reset email failed for %s', email)

        return Response({
            'ok': True,
            'message': 'אם כתובת האימייל קיימת במערכת, נשלח אליך קישור לאיפוס סיסמה.',
        })


class ResetPasswordView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({
            'ok': True,
            'message': 'הסיסמה עודכנה בהצלחה. ניתן להתחבר עם הסיסמה החדשה.',
        })


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Invalidate token (simple + safe)
        Token.objects.filter(user=request.user).delete()

        response = Response({'ok': True}, status=status.HTTP_200_OK)
        response.delete_cookie('auth_token', path='/')
        return response


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'user': CurrentUserSerializer(request.user).data}, status=status.HTTP_200_OK)


class UserViewSet(viewsets.ModelViewSet):
    """
    Manager-only CRUD for internal users.
    """

    queryset = User.objects.all().select_related('profile').order_by('email')
    serializer_class = ManagedUserSerializer
    permission_classes = [IsAuthenticated, IsManager]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['request'] = self.request
        return ctx


class CompleteTourView(APIView):
    """
    Mark the guided tour as finished for the signed-in user.

    POST /api/v1/core/auth/complete-tour/

    Called when the user finishes the last step or skips. Once set, the tour
    never opens on its own again — it stays reachable from the menu.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = getattr(request.user, 'profile', None)
        if profile is None:
            return Response({'ok': True, 'tour_completed': False}, status=status.HTTP_200_OK)

        if profile.tour_completed_at is None:
            profile.tour_completed_at = timezone.now()
            profile.save(update_fields=['tour_completed_at'])

        return Response({'ok': True, 'tour_completed': True}, status=status.HTTP_200_OK)


class LinkedUsersView(APIView):
    """
    Accounts the signed-in user may also look at.

    GET    /api/v1/core/auth/linked-users/            → my own links
    GET    ?user_id=<id>                              → someone else's (manager only)
    POST   {user_id, linked_user_id}                  → grant   (manager only)
    DELETE ?user_id=<id>&linked_user_id=<id>          → revoke  (manager only)

    Granting is a manager action: an instructor can use a link but can never
    create one for themselves.
    """

    permission_classes = [IsAuthenticated]

    def _is_manager(self, user):
        return getattr(getattr(user, 'profile', None), 'role', None) == UserProfile.ROLE_MANAGER

    def _serialize(self, user):
        name = f"{user.first_name} {user.last_name}".strip()
        return {
            'id': str(user.id),
            'name': name or user.username,
            'username': user.username,
            'email': user.email,
            'role': getattr(getattr(user, 'profile', None), 'role', None),
        }

    def get(self, request):
        from apps.core.models import LinkedUserAccess

        owner = request.user
        requested = request.query_params.get('user_id')
        if requested and str(requested) != str(request.user.id):
            if not self._is_manager(request.user):
                return Response({'detail': 'אין הרשאה.'}, status=status.HTTP_403_FORBIDDEN)
            owner = User.objects.filter(pk=requested).first()
            if owner is None:
                return Response({'detail': 'משתמש לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        links = (
            LinkedUserAccess.objects
            .filter(owner=owner)
            .select_related('linked', 'linked__profile')
            .order_by('linked__first_name', 'linked__username')
        )
        return Response({
            'user': self._serialize(owner),
            'linked_users': [self._serialize(link.linked) for link in links],
        })

    def post(self, request):
        from apps.core.models import LinkedUserAccess

        if not self._is_manager(request.user):
            return Response({'detail': 'אין הרשאה.'}, status=status.HTTP_403_FORBIDDEN)

        owner_id = request.data.get('user_id')
        linked_id = request.data.get('linked_user_id')
        if not owner_id or not linked_id:
            return Response({'detail': 'חסרים שדות'}, status=status.HTTP_400_BAD_REQUEST)
        if str(owner_id) == str(linked_id):
            return Response({'detail': 'לא ניתן לקשר משתמש לעצמו'}, status=status.HTTP_400_BAD_REQUEST)

        owner = User.objects.filter(pk=owner_id).first()
        linked = User.objects.filter(pk=linked_id).first()
        if owner is None or linked is None:
            return Response({'detail': 'משתמש לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        LinkedUserAccess.objects.get_or_create(
            owner=owner, linked=linked, defaults={'created_by': request.user}
        )
        return Response(self._serialize(linked), status=status.HTTP_201_CREATED)

    def delete(self, request):
        from apps.core.models import LinkedUserAccess

        if not self._is_manager(request.user):
            return Response({'detail': 'אין הרשאה.'}, status=status.HTTP_403_FORBIDDEN)

        owner_id = request.query_params.get('user_id')
        linked_id = request.query_params.get('linked_user_id')
        if not owner_id or not linked_id:
            return Response({'detail': 'חסרים שדות'}, status=status.HTTP_400_BAD_REQUEST)

        LinkedUserAccess.objects.filter(owner_id=owner_id, linked_id=linked_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
