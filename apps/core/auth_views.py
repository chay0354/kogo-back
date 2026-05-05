from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.auth import get_user_model
from apps.core.auth_serializers import LoginSerializer, CurrentUserSerializer, ManagedUserSerializer
from apps.core.permissions import IsManager


User = get_user_model()


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        user = authenticate(request, email=email, password=password)
        if not user:
            return Response({'error': 'אימייל או סיסמה שגויים'}, status=status.HTTP_401_UNAUTHORIZED)

        # Create or reuse token
        token, _ = Token.objects.get_or_create(user=user)

        response = Response(
            {
                'user': CurrentUserSerializer(user).data,
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


