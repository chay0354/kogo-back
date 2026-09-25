from datetime import timedelta

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

# How stale a session's "last used" may get before a request writes it again.
# Every request writing it would be one more write per click for nothing.
LAST_USED_RESOLUTION = timedelta(minutes=10)


class CookieTokenAuthentication(TokenAuthentication):
    """
    Token auth that supports reading the DRF token from:
    - Authorization: Token <key>
    - Cookie: auth_token=<key>

    Two kinds of key are accepted. A per-device key (LoginSession, "kd_…") is
    what every sign-in hands out now. The old shared key (DRF Token, one per
    user) still works for devices signed in before, until that user signs out.
    """

    cookie_name = 'auth_token'

    def authenticate(self, request):
        # 1) Standard DRF header token
        header_auth = super().authenticate(request)
        if header_auth:
            return header_auth

        # 2) Cookie token
        token = request.COOKIES.get(self.cookie_name)
        if not token:
            return None

        return self.authenticate_credentials(token)

    def authenticate_credentials(self, key):
        from apps.core.models import LoginSession

        if not LoginSession.is_device_key(key):
            return super().authenticate_credentials(key)

        session = (
            LoginSession.objects.select_related('user')
            .filter(key_hash=LoginSession.hash_key(key))
            .first()
        )
        if session is None:
            raise exceptions.AuthenticationFailed('Invalid token.')
        if not session.user.is_active:
            raise exceptions.AuthenticationFailed('User inactive or deleted.')

        now = timezone.now()
        if session.last_used_at is None or now - session.last_used_at > LAST_USED_RESOLUTION:
            LoginSession.objects.filter(pk=session.pk).update(last_used_at=now)
            session.last_used_at = now
        return (session.user, session)
