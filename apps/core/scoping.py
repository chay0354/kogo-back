"""
Course and branch visibility scoping.

Managers see all courses and derived data.
Partners see data only for explicitly assigned branches.
Instructor users (worker role, matched to Instructor by email) see only
courses where they teach at least one lesson.
"""
from __future__ import annotations

from django.db.models import Q

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
    """Managers are never course-scoped (kept for call-site compatibility)."""
    return False


def is_scoped_partner(user) -> bool:
    """True when this partner user should only see assigned branches."""
    if not user or not user.is_authenticated:
        return False
    if is_unrestricted(user):
        return False
    return get_user_role(user) == UserProfile.ROLE_PARTNER


def is_scoped_instructor(user) -> bool:
    """True when this worker user should only see their assigned courses."""
    if not user or not user.is_authenticated:
        return False
    if is_unrestricted(user):
        return False
    return get_user_role(user) == UserProfile.ROLE_WORKER


def partner_branch_ids(user):
    """Branch ids explicitly assigned to a partner user."""
    if not is_scoped_partner(user):
        return []
    try:
        return list(user.profile.assigned_branches.values_list('id', flat=True))
    except UserProfile.DoesNotExist:
        return []


def instructor_course_ids(user):
    """Course ids where this instructor user is assigned to the team."""
    from apps.courses.models import Course

    if not user or not user.email:
        return []
    return list(
        Course.objects.filter(instructor__email__iexact=user.email)
        .values_list('id', flat=True)
        .distinct()
    )


def partner_course_ids(user):
    """Course ids in branches assigned to a partner."""
    from apps.courses.models import Course

    branch_ids = partner_branch_ids(user)
    if not branch_ids:
        return []
    return list(
        Course.objects.filter(branch_id__in=branch_ids)
        .values_list('id', flat=True)
        .distinct()
    )


def assigned_course_ids(user):
    """Course ids visible to a scoped user; empty for managers."""
    if is_scoped_partner(user):
        return partner_course_ids(user)
    if is_scoped_instructor(user):
        return instructor_course_ids(user)
    return []


def assigned_branch_ids(user):
    """Branch ids visible to a scoped user."""
    if is_scoped_partner(user):
        return partner_branch_ids(user)
    if is_scoped_instructor(user):
        from apps.courses.models import Course

        ids = instructor_course_ids(user)
        if not ids:
            return []
        return list(
            Course.objects.filter(id__in=ids)
            .values_list('branch_id', flat=True)
            .distinct()
        )
    return []


def scope_courses(qs, user, course_lookup=''):
    """
    Restrict a queryset to courses visible to `user`.

    Managers and superusers: no filtering.
    Partners: courses in assigned branches.
    Instructor users (workers): only courses where they teach a lesson.

    course_lookup is the ORM path from the model to Course:
      - ''              for a Course queryset (filter on pk)
      - 'course'        for Lesson
      - 'lesson__course' for LessonEnrollment / attendance
      - 'initial_payment__lesson__course' for RecurringPayment, etc.
    """
    if is_scoped_partner(user):
        ids = partner_course_ids(user)
        if not ids:
            return qs.none()
        if course_lookup in ('', None):
            return qs.filter(pk__in=ids)
        return qs.filter(**{f'{course_lookup}_id__in': ids})

    if not is_scoped_instructor(user):
        return qs
    ids = instructor_course_ids(user)
    if not ids:
        return qs.none()
    if course_lookup in ('', None):
        return qs.filter(pk__in=ids)
    return qs.filter(**{f'{course_lookup}_id__in': ids})


def scope_branches(qs, user, branch_lookup=''):
    """Restrict a queryset to branches visible to `user`."""
    if is_scoped_partner(user):
        ids = partner_branch_ids(user)
        if not ids:
            return qs.none()
        if branch_lookup in ('', None):
            return qs.filter(pk__in=ids)
        return qs.filter(**{f'{branch_lookup}_id__in': ids})

    if not is_scoped_instructor(user):
        return qs
    ids = assigned_branch_ids(user)
    if not ids:
        return qs.none()
    if branch_lookup in ('', None):
        return qs.filter(pk__in=ids)
    return qs.filter(**{f'{branch_lookup}_id__in': ids})


def scope_store_products(qs, user):
    """Restrict store products to branches assigned to a partner."""
    if not is_scoped_partner(user):
        return qs
    ids = partner_branch_ids(user)
    if not ids:
        return qs.none()
    return qs.filter(
        Q(branch_id__in=ids) | Q(size_stocks__branch_id__in=ids)
    ).distinct()


def scope_instructors(qs, user):
    """Restrict instructors to those linked to partner branches."""
    if not is_scoped_partner(user):
        return qs
    ids = partner_branch_ids(user)
    if not ids:
        return qs.none()
    return qs.filter(
        Q(primary_branch_id__in=ids)
        | Q(branch_assignments__branch_id__in=ids)
        | Q(lessons__course__branch_id__in=ids)
    ).distinct()
