"""
Course and branch visibility scoping.

Managers see all courses and derived data.
Partners see data only for explicitly assigned branches.
Instructor users (worker role, matched to Instructor by login username
or email) see only courses where they teach at least one lesson.
"""
from __future__ import annotations

from typing import NamedTuple

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


def user_login_idents(user):
    """Email and/or username the worker may log in with."""
    idents = []
    if not user:
        return idents
    for val in (getattr(user, 'email', None), getattr(user, 'username', None)):
        ident = (val or '').strip()
        if ident and ident not in idents:
            idents.append(ident)
    return idents


def instructor_login_q(user, instructor_field='instructor'):
    """
    Match Instructor.email (stored login username) to the user's email or username.
    instructor_field='instructor' → Lesson/Course; '' → Instructor queryset.
    """
    idents = user_login_idents(user)
    if not idents:
        return Q(pk__in=[])
    key = f'{instructor_field}__email__iexact' if instructor_field else 'email__iexact'
    q = Q()
    for ident in idents:
        q |= Q(**{key: ident})
    return q


def instructor_for_user(user):
    """Instructor record whose login username matches this worker user."""
    from apps.instructors.models import Instructor

    if not user_login_idents(user):
        return None
    return Instructor.objects.filter(instructor_login_q(user, instructor_field='')).first()


def instructor_course_ids(user):
    """Course ids where this instructor user is assigned to the team."""
    from apps.courses.models import Course

    if not user_login_idents(user):
        return []
    return list(
        Course.objects.filter(instructor_login_q(user))
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


def partner_instructor_ids(user):
    """Instructor ids linked to a partner's assigned branches."""
    from apps.instructors.models import Instructor

    ids = partner_branch_ids(user)
    if not ids:
        return []
    return list(
        Instructor.objects.filter(
            Q(primary_branch_id__in=ids)
            | Q(branch_assignments__branch_id__in=ids)
            | Q(lessons__course__branch_id__in=ids)
        )
        .values_list('id', flat=True)
        .distinct()
    )


ACTIVE_ENROLLMENT_STATUSES = ('active', 'payments_problem')


def partner_visible_children_q(branch_ids):
    """
    Children visible to a partner on the customers page:
    - families registered at an assigned branch, or
    - active lesson enrollments at an assigned branch.
    """
    return Q(family__branch_id__in=branch_ids) | Q(
        lesson_enrollments__lesson__course__branch_id__in=branch_ids,
        lesson_enrollments__status__in=ACTIVE_ENROLLMENT_STATUSES,
    )


def partner_child_display_branch(child, branch_ids):
    """
    Branch id/name to show a partner for a child — prefer an active enrollment
    at one of the partner's branches over the family's home branch elsewhere.
    """
    enrollment = (
        child.lesson_enrollments.filter(
            status__in=ACTIVE_ENROLLMENT_STATUSES,
            lesson__course__branch_id__in=branch_ids,
        )
        .select_related('lesson__course__branch')
        .order_by('-updated_at')
        .first()
    )
    if enrollment and enrollment.lesson.course.branch_id:
        branch = enrollment.lesson.course.branch
        return branch.id, branch.name
    if child.family.branch_id in branch_ids and child.family.branch_id:
        branch = child.family.branch
        return branch.id, branch.name if branch else None
    branch = child.family.branch
    return (branch.id if branch else None, branch.name if branch else None)


def scope_cities(qs, user):
    """Restrict cities to those with at least one branch visible to `user`."""
    if is_scoped_partner(user):
        ids = partner_branch_ids(user)
        if not ids:
            return qs.none()
        return qs.filter(branches__id__in=ids, branches__is_active=True).distinct()

    if is_scoped_instructor(user):
        ids = assigned_branch_ids(user)
        if not ids:
            return qs.none()
        return qs.filter(branches__id__in=ids, branches__is_active=True).distinct()

    return qs


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


def linked_user_ids(user):
    """
    Accounts this user was granted read access to. Never includes themselves.

    Answers only *which* accounts, never *how much* of them — a link may be
    limited to one branch. Nothing that decides what may be read should be
    built on this; use resolve_viewable_user, which carries the limit.
    """
    from apps.core.models import LinkedUserAccess

    if not user or not user.is_authenticated:
        return set()
    return set(
        LinkedUserAccess.objects.filter(owner=user).values_list('linked_id', flat=True)
    )


class ViewableSubject(NamedTuple):
    """
    The account a read request runs as, and how far that reaches.

    Both answers come out of the same check, so they travel together: a link
    limited to one branch must never be able to hand a caller the account
    without also handing it the limit. Filter through the methods here rather
    than rebuilding a filter from `.user`, which would drop the limit silently.
    """

    user: object
    branch_id: object = None

    def branch_q(self, branch_lookup='course__branch_id') -> Q:
        """The branch limit alone, for a queryset narrowed by identity elsewhere."""
        if not self.branch_id:
            return Q()
        return Q(**{branch_lookup: self.branch_id})

    def lesson_q(self) -> Q:
        """Lessons this request may reach: whose they are, and where."""
        return instructor_login_q(self.user) & self.branch_q()


def resolve_viewable_user(request, as_user_id):
    """
    Who a read request is allowed to run as, and in which branch.

    Returns a ViewableSubject for `request.user` when no other account was asked
    for. Otherwise the requested account, but only if a manager asked, or a
    LinkedUserAccess row grants it. Anything else raises PermissionDenied — the
    id in the query string is a request, never a permission.

    A row that names a branch grants that branch only, and the subject carries
    that limit so no call site has to remember it. A manager is not limited this
    way: they already reach every branch without a row.

    Callers decide what the resolved subject may then do. On the lesson queryset
    it widens both reading a register and marking it, which is intended: an
    instructor covering a colleague has to be able to mark. Do not reuse this to
    widen anything a manager alone should reach.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError
    from rest_framework.exceptions import PermissionDenied

    from apps.core.models import LinkedUserAccess, UserProfile

    user = request.user
    if not as_user_id or str(as_user_id) == str(user.id):
        return ViewableSubject(user)

    User = get_user_model()
    try:
        target = User.objects.filter(pk=as_user_id).first()
    except (ValueError, TypeError, ValidationError):
        # A malformed id is a bad request from an untrusted string, not a
        # server error. Treat it the same as an id that grants nothing.
        raise PermissionDenied('אין הרשאה לצפות במשתמש הזה')
    if target is None:
        raise PermissionDenied('משתמש לא נמצא')

    role = getattr(getattr(user, 'profile', None), 'role', None)
    if role == UserProfile.ROLE_MANAGER:
        return ViewableSubject(target)

    link = LinkedUserAccess.objects.filter(owner=user, linked=target).first()
    if link is not None:
        return ViewableSubject(target, link.branch_id)

    raise PermissionDenied('אין הרשאה לצפות במשתמש הזה')


def integration_credential(name):
    """
    A credential, taken from the environment when it is set there and otherwise
    from the row a manager filled in.

    The environment always wins, so adding the variable to the deployment
    silently retires the stored copy rather than competing with it.
    """
    from django.conf import settings as django_settings

    from apps.core.models import IntegrationCredential

    value = (getattr(django_settings, name, '') or '').strip()
    if value:
        return value
    stored = IntegrationCredential.objects.filter(key=name).values_list('value', flat=True).first()
    return (stored or '').strip()
