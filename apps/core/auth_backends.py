from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q


UserModel = get_user_model()


class EmailBackend(ModelBackend):
    """
    Authenticate using email or username + password.
    Instructors may log in with a free-form username (not a real email).
    """

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        login_id = (email or username or kwargs.get('email') or '').strip()
        if not login_id or not password:
            return None

        user = UserModel.objects.filter(
            Q(email__iexact=login_id) | Q(username__iexact=login_id)
        ).first()
        if user is None:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


