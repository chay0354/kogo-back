from rest_framework.permissions import BasePermission

from apps.core.models import UserProfile


class IsManager(BasePermission):
    """
    Allows access only to authenticated users with role=manager.
    """

    message = 'אין הרשאה. נדרש תפקיד מנהל.'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False

        try:
            return user.profile.role == UserProfile.ROLE_MANAGER
        except UserProfile.DoesNotExist:
            return False


