"""
Combined-track (LessonBundle) helpers shared by the public widget catalog
and subscription billing.

The widget can still show a must-attend course after its only bundle was
accidentally deactivated. Billing must accept that same bundle, otherwise
parents see "Lesson bundle not found or inactive" on those lessons.
"""
from django.core.exceptions import ValidationError

from apps.courses.models import LessonBundle


def catalog_bundles_for_course(course):
    """
    Bundles exposed in the public widget catalog.

    For must_attend_all_lessons courses, include an inactive bundle when it is
    the only bundle with lessons (common after accidental soft-delete).
    """
    candidates = [b for b in course.lesson_bundles.all() if b.lessons.exists()]
    active = [b for b in candidates if b.is_active]
    if active:
        return active
    if course.must_attend_all_lessons and candidates:
        return candidates
    return []


def resolve_registration_bundle(*, course, bundle_id: str):
    """
    Bundle the public widget (and billing) may register against.

    Inactive bundles are allowed only as a must-attend fallback when no
    active bundle with lessons remains — same rule as the catalog.
    """
    try:
        bundle = (
            LessonBundle.objects
            .prefetch_related('lessons')
            .get(id=bundle_id, course=course)
        )
    except (LessonBundle.DoesNotExist, ValidationError, ValueError, TypeError):
        return None
    if not bundle.lessons.exists():
        return None
    if bundle.is_active:
        return bundle
    if not course.must_attend_all_lessons:
        return None
    has_active = (
        LessonBundle.objects
        .filter(course=course, is_active=True)
        .filter(lessons__isnull=False)
        .exists()
    )
    if has_active:
        return None
    return bundle
