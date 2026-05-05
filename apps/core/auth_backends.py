from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model


UserModel = get_user_model()


class EmailBackend(ModelBackend):
    """
    Authenticate using email + password.
    Keeps Django's default user model (no custom AUTH_USER_MODEL migration required).
    """

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        login_email = email or username or kwargs.get('email')
        if not login_email or not password:
            return None

        try:
            user = UserModel.objects.get(email__iexact=login_email)
        except UserModel.DoesNotExist:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


