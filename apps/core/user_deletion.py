"""
What deleting an internal user actually costs, and the reasons not to.

The screen already deactivates an account, which is reversible and keeps every
name attached to the work that person did. Deletion is the other thing, and it
is worth knowing what it takes with it before anyone presses it:

* No customer, payment, document or signature is lost. Every `created_by` /
  `changed_by` / `marked_by` field pointing at a user is SET_NULL, so the
  records stay and only stop saying who did it.
* What is destroyed is the account itself, its API token, its role, and the
  linked-access grants it gave or received.
* An instructor is matched to an account by e-mail string, not by a foreign
  key, so deleting the account leaves the instructor record in place and
  silently removes their only way to sign in. That one is worth a warning.

Two deletions are refused outright: your own account, and the last manager who
can still sign in — an empty-handed system nobody can administer is not a state
a single click should be able to reach.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Count

from apps.core.models import LinkedUserAccess, UserProfile


class UserDeletionRefused(Exception):
    """Raised with a Hebrew message explaining why this account may not be deleted."""


def _user_identifiers(user) -> set[str]:
    return {
        value.strip().casefold()
        for value in (user.email, user.username)
        if (value or '').strip()
    }


def linked_instructor(user):
    """The Instructor record this account signs in as, matched the way login matches it."""
    from apps.instructors.models import Instructor

    idents = _user_identifiers(user)
    if not idents:
        return None
    for instructor in Instructor.objects.all():
        key = (instructor.email or '').strip().casefold()
        if key and key in idents:
            return instructor
    return None


def role_of(user) -> str | None:
    """The stored role, read from the table.

    Deliberately not `user.profile.role`: a User instance that was handed a
    profile earlier in the request keeps the role it was created with, and a
    refusal that depends on being a manager must not turn on a cached value.
    """
    return UserProfile.objects.filter(user=user).values_list('role', flat=True).first()


def active_manager_count(exclude_id=None) -> int:
    qs = UserProfile.objects.filter(role=UserProfile.ROLE_MANAGER, user__is_active=True)
    if exclude_id is not None:
        qs = qs.exclude(user_id=exclude_id)
    return qs.count()


def refusal_reason(user, *, actor) -> str:
    """The Hebrew reason this account may not be deleted, or '' when it may."""
    if actor is not None and user.pk == actor.pk:
        return 'אי אפשר למחוק את החשבון שאיתו אתה מחובר'

    role = role_of(user)
    if role == UserProfile.ROLE_MANAGER and user.is_active and active_manager_count(exclude_id=user.pk) == 0:
        return 'זה המנהל הפעיל האחרון — מחיקה תשאיר את המערכת בלי אף אחד שיכול לנהל אותה'
    return ''


def deletion_preview(user, *, actor=None) -> dict:
    """Everything the confirmation screen needs, without deleting anything."""
    from apps.courses.models import Course

    instructor = linked_instructor(user)
    lessons = 0
    if instructor is not None:
        lessons = (
            Course.objects.filter(lessons__instructor=instructor, is_active=True)
            .aggregate(n=Count('lessons', distinct=True))['n'] or 0
        )

    return {
        'id': user.pk,
        'name': (f'{user.first_name} {user.last_name}'.strip() or user.email or user.username),
        'email': user.email or user.username,
        'role': role_of(user),
        'is_active': user.is_active,
        'refusal': refusal_reason(user, actor=actor),
        'instructor': None if instructor is None else {
            'id': str(instructor.id),
            'name': instructor.full_name if hasattr(instructor, 'full_name') else str(instructor),
            'active_lessons': lessons,
        },
        'linked_access_granted': LinkedUserAccess.objects.filter(owner=user).count(),
        'linked_access_received': LinkedUserAccess.objects.filter(linked=user).count(),
        # Said plainly because it is the part people get wrong: nothing the
        # person did disappears, it only stops carrying their name.
        'history_note': (
            'הנתונים שהמשתמש יצר נשארים במערכת — חיובים, מסמכים, שינויי סטטוס והתאמות מלאי. '
            'מה שמשתנה הוא שהם יפסיקו לשאת את שמו.'
        ),
    }


def delete_user(user, *, actor=None) -> dict:
    """Delete after the refusals, and report what it did."""
    reason = refusal_reason(user, actor=actor)
    if reason:
        raise UserDeletionRefused(reason)

    summary = deletion_preview(user, actor=actor)
    User = get_user_model()
    User.objects.filter(pk=user.pk).delete()
    return {
        'deleted': True,
        'name': summary['name'],
        'email': summary['email'],
        'instructor_left_without_login': summary['instructor'] is not None,
    }
