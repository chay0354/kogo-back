"""Shared WhatsApp context for lesson registration / trial flows."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from apps.courses.models import Lesson

if TYPE_CHECKING:
    from apps.customers.models import Child, Payment


def build_enrollment_whatsapp_context(
    *,
    child: 'Child',
    lesson: Lesson,
) -> Optional[dict]:
    """Build parent/lesson fields for enrollment-related WhatsApp messages."""
    family = child.family if child else None
    if not family:
        return None

    primary_parent = (
        family.parents.filter(is_primary=True).first()
        or family.parents.first()
    )
    parent_phone = (primary_parent.phone if primary_parent else '') or family.phone or ''
    parent_phone = (parent_phone or '').strip()
    if not parent_phone:
        return None

    if primary_parent:
        parent_name = f"{primary_parent.first_name} {primary_parent.last_name}".strip()
    else:
        parent_name = family.name or ''

    lookup_names: list[str] = []

    def _add_name(value: str | None) -> None:
        n = (value or '').strip()
        if not n:
            return
        if n not in lookup_names:
            lookup_names.append(n)
        for word in n.split():
            if len(word) >= 2 and word not in lookup_names:
                lookup_names.append(word)

    _add_name(family.name)
    _add_name(parent_name)
    if primary_parent:
        _add_name(primary_parent.first_name)
        _add_name(primary_parent.last_name)

    return {
        'phone': parent_phone,
        'parent_name': parent_name or family.name,
        'lookup_names': lookup_names,
        'child_name': f"{child.first_name} {child.last_name}".strip(),
        'course_name': lesson.course.name if lesson.course_id else '',
        'branch_name': (
            lesson.course.branch.name
            if lesson.course_id and lesson.course.branch_id
            else ''
        ),
        'day_name': dict(Lesson.DAY_OF_WEEK_CHOICES).get(lesson.day_of_week, ''),
        'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else '',
        'end_time': lesson.end_time.strftime('%H:%M') if lesson.end_time else '',
    }


def build_enrollment_whatsapp_context_from_payment(payment: 'Payment') -> Optional[dict]:
    """Build context from a subscription payment record."""
    lesson = payment.lesson
    if not lesson:
        return None
    child = payment.child
    if not child:
        return None
    return build_enrollment_whatsapp_context(child=child, lesson=lesson)
