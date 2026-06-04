"""
Per-manager course scoping.

Managers are restricted to the courses they are assigned to (Course.managers).
A manager sees only those courses — and everything derived from them (lessons,
calendar, students, payments, dashboard, store, salaries). A manager with no
assigned courses sees nothing.

Django superusers bypass all scoping (safety/escape hatch so assignments can
always be fixed, and so the system can never be permanently locked out).
"""
from __future__ import annotations

from apps.core.models import UserProfile


def get_user_role(user):
    """Return the UserProfile role string, or None."""
    try:
        return user.profile.role
    except (UserProfile.DoesNotExist, AttributeError):
        return None


def is_unrestricted(user) -> bool:
    """Superusers are never course-scoped."""
    return bool(getattr(user, 'is_superuser', False))


def is_scoped_manager(user) -> bool:
    """True when this user's queries must be restricted to assigned courses."""
    if not user or not user.is_authenticated:
        return False
    if is_unrestricted(user):
        return False
    return get_user_role(user) == UserProfile.ROLE_MANAGER


def assigned_course_ids(user):
    """List of course ids assigned to this manager."""
    from apps.courses.models import Course
    return list(
        Course.objects.filter(managers=user).values_list('id', flat=True)
    )


def assigned_branch_ids(user):
    """Distinct branch ids derived from the manager's assigned courses."""
    from apps.courses.models import Course
    return list(
        Course.objects.filter(managers=user)
        .values_list('branch_id', flat=True)
        .distinct()
    )


def scope_courses(qs, user, course_lookup=''):
    """
    Restrict a queryset to objects whose related Course is assigned to `user`.

    course_lookup is the ORM path from the model to Course:
      - ''              for a Course queryset (filter on pk)
      - 'course'        for Lesson
      - 'lesson__course' for LessonEnrollment / attendance
      - 'initial_payment__lesson__course' for RecurringPayment, etc.

    Superusers and non-managers (workers handled elsewhere) are returned as-is.
    """
    if not is_scoped_manager(user):
        return qs
    ids = assigned_course_ids(user)
    if course_lookup in ('', None):
        return qs.filter(pk__in=ids)
    return qs.filter(**{f'{course_lookup}_id__in': ids})


def scope_branches(qs, user, branch_lookup=''):
    """Restrict a queryset to the manager's assigned branches."""
    if not is_scoped_manager(user):
        return qs
    ids = assigned_branch_ids(user)
    if branch_lookup in ('', None):
        return qs.filter(pk__in=ids)
    return qs.filter(**{f'{branch_lookup}_id__in': ids})
