"""
Tracks taught by two instructors, and the photo that belongs to the two of them.

The widget sells a combined track — twice a week, three times a week — as one
thing, and the days inside it are not always taught by the same person. One
circle beside the track cannot be one of two faces, so a pair that teaches
together gets a picture of the two of them, stored once for the pair.

A pair is discovered from the schedule rather than typed in: whoever teaches the
days of one track is a pair, and stops being one the moment the schedule changes.
"""
from __future__ import annotations

from apps.courses.models import Course, LessonBundle
from apps.instructors.models import InstructorPairPhoto


def _pair_from_lessons(lessons) -> tuple | None:
    """The two instructors of a combined track, or None when it is not two."""
    instructors = {}
    for lesson in lessons:
        if lesson.instructor_id:
            instructors[lesson.instructor_id] = lesson.instructor
    if len(instructors) != 2:
        return None
    return tuple(instructors.values())


def combined_tracks(instructor=None) -> list[dict]:
    """
    Every track that two instructors teach together.

    A track is a מסלול משולב, or a course whose days must all be attended —
    both are one thing to the parent, and both show one instructor circle.
    """
    tracks: list[dict] = []

    bundles = (
        LessonBundle.objects
        .filter(is_active=True)
        .select_related('course', 'course__branch')
        .prefetch_related('lessons__instructor')
    )
    for bundle in bundles:
        pair = _pair_from_lessons(bundle.lessons.all())
        if pair is None:
            continue
        tracks.append({
            'kind': 'bundle',
            'id': str(bundle.id),
            'name': bundle.name or bundle.course.name,
            'course_id': str(bundle.course_id),
            'course_name': bundle.course.name,
            'course_display_id': bundle.course.display_id,
            'branch_name': getattr(bundle.course.branch, 'name', '') or '',
            'instructors': pair,
        })

    # A course sold as a bundle is already listed; listing it twice would ask
    # for the same photo twice.
    bundled_course_ids = {track['course_id'] for track in tracks}
    courses = (
        Course.objects
        .filter(is_active=True, must_attend_all_lessons=True)
        .select_related('branch')
        .prefetch_related('lessons__instructor')
    )
    for course in courses:
        if str(course.id) in bundled_course_ids:
            continue
        pair = _pair_from_lessons(course.lessons.all())
        if pair is None:
            continue
        tracks.append({
            'kind': 'course',
            'id': str(course.id),
            'name': course.name,
            'course_id': str(course.id),
            'course_name': course.name,
            'course_display_id': course.display_id,
            'branch_name': getattr(course.branch, 'name', '') or '',
            'instructors': pair,
        })

    if instructor is not None:
        instructor_id = str(getattr(instructor, 'id', instructor))
        tracks = [
            track for track in tracks
            if any(str(person.id) == instructor_id for person in track['instructors'])
        ]
    return tracks


def partners_for_instructor(instructor) -> list[dict]:
    """
    Who this instructor shares a combined track with, and the photo of the two.

    One row per partner, with the tracks they share, so the CRM can offer one
    upload per partner rather than one per track.
    """
    by_partner: dict = {}
    for track in combined_tracks(instructor):
        partner = next(
            person for person in track['instructors']
            if str(person.id) != str(instructor.id)
        )
        entry = by_partner.setdefault(str(partner.id), {
            'partner_id': str(partner.id),
            'partner_name': partner.full_name,
            'partner_photo_url': partner.photo_url or None,
            'photo_url': None,
            'tracks': [],
        })
        entry['tracks'].append({
            'kind': track['kind'],
            'id': track['id'],
            'name': track['name'],
            'course_name': track['course_name'],
            'course_display_id': track['course_display_id'],
            'branch_name': track['branch_name'],
        })

    for partner_id, entry in by_partner.items():
        pair = InstructorPairPhoto.for_pair(instructor.id, partner_id)
        entry['photo_url'] = pair.photo_url if pair else None

    return sorted(by_partner.values(), key=lambda entry: entry['partner_name'])


def pair_photo_map() -> dict:
    """
    Every stored pair photo, keyed by the pair in its stored order.

    There are as many rows here as there are pairs teaching together — a
    handful — so a catalog of bundles reads them once instead of once each.
    """
    return {
        (str(row.first_instructor_id), str(row.second_instructor_id)): row.photo_url
        for row in InstructorPairPhoto.objects.all()
    }


def pair_photo_for_lessons(lessons, photo_map: dict | None = None) -> str | None:
    """The combined photo for a track's two instructors, when there is one."""
    pair = _pair_from_lessons(lessons)
    if pair is None:
        return None
    if photo_map is not None:
        first, second = InstructorPairPhoto.ordered_pair(*pair)
        return photo_map.get((str(first.id), str(second.id)))
    stored = InstructorPairPhoto.for_pair(*pair)
    return stored.photo_url if stored else None
