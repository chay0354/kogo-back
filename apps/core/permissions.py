from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated

from apps.core.models import UserProfile


class IsSuperUser(BasePermission):
    """Django superusers only — for devops-level operations (e.g. DB backups)."""

    message = 'אין הרשאה. פעולה זו מוגבלת למנהלי מערכת.'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        return bool(user and user.is_authenticated and user.is_superuser)


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


class IsManagerOrPartner(BasePermission):
    """Managers and partners (partners are scoped in querysets)."""

    message = 'אין הרשאה.'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        try:
            return user.profile.role in (UserProfile.ROLE_MANAGER, UserProfile.ROLE_PARTNER)
        except UserProfile.DoesNotExist:
            return False


class StaffAccessMixin:
    """Managers and partners can access operational endpoints."""

    def get_permissions(self):
        return [IsAuthenticated(), IsManagerOrPartner()]


class ManagerWriteMixin:
    """Partners read; managers read and write."""

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated(), IsManagerOrPartner()]
        return [IsAuthenticated(), IsManager()]
