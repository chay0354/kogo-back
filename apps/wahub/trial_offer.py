"""
When a trial lesson can be booked — exactly as the registration form on the
site has it (docs/WAHUB-BOT-ACCURACY-2026-10-12.md).

The owner (12.10.2026): "and how exact is it against what the system knows —
the dates one can register on for a trial lesson, by the system, exactly". A
date the bot says a child can come on has to be a date the form would let that
parent choose at that moment, and book. Not one more and not one less.

The form decides in three places, and a date is offered here only when all
three agree. Each is asked through the form's own function; nothing of them is
worked out a second time here.

    the card of the class    is there a trial button at all
        back    WidgetCoursesView → _lesson_widget_capacity, trial_registration_open_for,
                catalog_bundles_for_course (apps/customers/widget_views.py)
        front   kogo-front src/app/widget: catalogRows.buildCatalogRows (which rows a class
                has), alternativeLessons.isWidgetSelectionFull (a full class shows no button),
                lessonVisibility.trialRegistrationOffered / trialLessonChoices,
                page.tsx handleEnrollClick (a class with a link of its own opens the link)
    the date picker          which dates, and which of them are shown "מלא"
        back    WidgetLessonOccurrencesView → iter_upcoming_lesson_occurrences (one lesson),
                iter_merged_upcoming_lesson_occurrences (a track), trial_seats_left
    the submit               what is refused even though it was shown
        back    WidgetTrialRegisterView → trial_registration_open_for, validate_trial_lesson_date,
                count_capacity_enrollments against the lesson's limit, a paid trial with no price

`apps/wahub/tests/test_trial_offer.py` holds this against the form itself: the
same data, asked through the form's public endpoints and booked through its
submit, gives the same dates.

One thing is said less than the form says it. The form keeps offering today's
lesson until the minute it ends; a parent told on WhatsApp "you can come today
at 17:00" at 17:20 was told nothing useful. A lesson of today that has already
begun is listed under `skipped`, not offered. Whether the form should stop too
is the owner's to decide.

Read only. Nothing here writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.utils import timezone

# Why no date can be booked for a class (or for one lesson of it).
HIDDEN = 'hidden'                # not in the form's catalogue
EXTERNAL_LINK = 'external_link'  # the class registers through a link of its own
NO_LESSONS = 'no_lessons'        # nothing on the form to choose
NO_PRICE = 'no_price'            # a paid trial with no price: the submit refuses it
CLOSED = 'closed'                # the office closed trial registration
CLASS_FULL = 'class_full'        # no seat for a subscriber: the card shows no button
TRACK_FULL = 'track_full'        # one day of a combined track is full, and the track with it
TRIAL_FULL = 'trial_full'        # every date the form offers is taken
NO_DATES = 'no_dates'            # no date ahead at all
STARTED = 'started'              # only today's lesson was left, and it has begun
TRACK_ONLY = 'track_only'        # the lesson is reached through a combined track, and the track is not offered
MIXED = 'mixed'                  # the lessons of the class have different reasons

# Why one date of a lesson is not among the offered ones.
SKIP_CANCELLED, SKIP_BLOCKED, SKIP_STARTED = 'cancelled', 'blocked', 'started'

# How far ahead the dates that fell out are looked for when nothing is offered.
SKIPPED_WEEKS = 3


def clock(now=None):
    """The moment as the form reads it — local time — or None for the form's own clock."""
    if now is None:
        return None
    if timezone.is_naive(now):
        now = timezone.make_aware(now)
    return timezone.localtime(now)


@dataclass(frozen=True)
class Slot:
    """One row of the form's date picker: a lesson on a date."""
    lesson: object
    day: date
    seats_left: int | None      # None: no limit is set anywhere
    is_full: bool               # shown "מלא" on the picker, or refused by the submit


@dataclass(frozen=True)
class Skipped:
    """A date the lesson would have fallen on, and why the form does not offer it."""
    lesson: object
    day: date
    why: str                    # SKIP_CANCELLED | SKIP_BLOCKED | SKIP_STARTED
    reason: str = ''            # what the office wrote on a blocked date ("חופשת סוכות")


@dataclass
class Offer:
    """What the form offers for a trial in one class, now."""
    slots: list = field(default_factory=list)       # every picker row a parent reaches, by date
    skipped: list = field(default_factory=list)
    reason: str = ''                                # why nothing can be booked; '' when something can
    by_lesson: dict = field(default_factory=dict)   # lesson id → '' (a date can be booked) or the reason

    @property
    def open(self) -> list:
        return [slot for slot in self.slots if not slot.is_full]

    @property
    def full(self) -> list:
        return [slot for slot in self.slots if slot.is_full]

    @property
    def can_book(self) -> bool:
        return bool(self.open)


class Seats:
    """
    Who sits where, for a set of classes: the form's own counts, fetched once
    for all their lessons, on the clock of the conversation.
    """

    def __init__(self, courses, *, now=None):
        from apps.customers.widget_views import (
            _batch_occurrence_context,
            _batch_paying_enrollment_counts,
            _batch_upcoming_trial_counts,
        )
        from apps.enrollments.trial_policy import trials_open_by_default

        # As the catalogue collects them: the lessons of each class, and those of its tracks.
        lesson_ids = []
        for course in courses:
            lesson_ids.extend(lesson.id for lesson in course.lessons.all())
            for bundle in course.lesson_bundles.all():
                lesson_ids.extend(member.id for member in bundle.lessons.all())
        self.lesson_ids = list(dict.fromkeys(lesson_ids))
        self.now = clock(now)
        self.enrolled = _batch_paying_enrollment_counts(self.lesson_ids)
        self.trials = _batch_upcoming_trial_counts(self.lesson_ids)
        self.occurrences = _batch_occurrence_context(self.lesson_ids)
        self.trials_default = trials_open_by_default()
        self._caps: dict = {}
        self._rosters: dict | None = None
        self._block_reasons: dict | None = None

    def cap(self, lesson, course) -> dict:
        """The seats of a lesson as the card of the class shows them."""
        from apps.customers.widget_views import _lesson_widget_capacity

        if lesson.id not in self._caps:
            self._caps[lesson.id] = _lesson_widget_capacity(
                lesson, course, self.enrolled, self.trials, self.occurrences, now=self.now,
            )
        return self._caps[lesson.id]

    def trial_open(self, lesson) -> bool:
        from apps.enrollments.trial_policy import trial_registration_open_for

        return trial_registration_open_for(lesson, default=self.trials_default)

    def roster(self, lesson) -> list:
        """The active rows of a lesson — the ones the picker and the submit count — read once for every lesson."""
        if self._rosters is None:
            from apps.enrollments.models import LessonEnrollment

            self._rosters = {}
            rows = LessonEnrollment.objects.filter(
                lesson_id__in=self.lesson_ids, status='active',
            ).select_related('child', 'child__family')
            for row in rows:
                self._rosters.setdefault(row.lesson_id, []).append(row)
        return self._rosters.get(lesson.id, [])

    def dates(self, lesson, *, count=None) -> list:
        """The coming dates of one lesson, as the picker lists them."""
        from apps.enrollments.trial_reminders import TRIAL_LESSON_OCCURRENCE_LIMIT, iter_upcoming_lesson_occurrences

        cancelled, blocked = self.occurrences
        return iter_upcoming_lesson_occurrences(
            lesson, count=count or TRIAL_LESSON_OCCURRENCE_LIMIT, now=self.now,
            cancelled=cancelled.get(lesson.id, set()), blocked=blocked.get(lesson.id),
        )

    def block_reason(self, day: date) -> str:
        """What the office wrote beside a date it closed to trials."""
        if self._block_reasons is None:
            from apps.enrollments.models import TrialBlockedDate

            self._block_reasons = {row.date: (row.reason or '').strip() for row in TrialBlockedDate.objects.all()}
        return self._block_reasons.get(day, '')


def in_catalogue(course) -> bool:
    """Whether the form lists the class at all (WidgetCoursesView)."""
    if not course.is_active or not course.show_in_widget:
        return False
    return course.course_type_id is None or bool(course.course_type.is_active)


def selections(course) -> list:
    """
    The rows a class has on the form, each (lesson, track) with one of the two
    empty — a parent opens one of these and nothing else (catalogRows.ts).
    A class whose every day is required has only its tracks; the instructors'
    track shows no single day and no track of one lesson.
    """
    from apps.courses import age_groups
    from apps.courses.bundles import catalog_bundles_for_course

    instructors = course.min_age == age_groups.INSTRUCTORS and course.max_age == age_groups.INSTRUCTORS
    rows = []
    if not course.must_attend_all_lessons and not instructors:
        rows += [(lesson, None) for lesson in course.lessons.all()]
    for bundle in catalog_bundles_for_course(course):
        members = list(bundle.lessons.all())
        if instructors and len(members) < 2:
            continue
        rows.append((None, bundle))
    return rows


def _refused_on_submit(lesson, course, day: date, seats: Seats) -> bool:
    """The capacity check of the submit, for a date the picker showed (WidgetTrialRegisterView)."""
    from apps.customers.widget_views import _resolve_lesson_capacity
    from apps.enrollments.enrollment_counts import count_capacity_enrollments

    capacity = _resolve_lesson_capacity(lesson, course) or course.capacity or 20
    taken = count_capacity_enrollments(
        lesson=lesson, occurrence_date=day, include_trials=True, enrollments=seats.roster(lesson),
    )
    return taken >= capacity


def _slot(lesson, course, day: date, seats: Seats) -> Slot:
    from apps.enrollments.enrollment_counts import trial_seats_left

    left = trial_seats_left(lesson=lesson, occurrence_date=day, enrollments=seats.roster(lesson))
    full = (left is not None and left <= 0) or _refused_on_submit(lesson, course, day, seats)
    return Slot(lesson=lesson, day=day, seats_left=left, is_full=full)


def _lesson_gate(lesson, course, seats: Seats) -> str:
    """Why the card of one lesson has no trial button; '' when it has one (lessonVisibility.trialRegistrationOffered)."""
    cap = seats.cap(lesson, course)
    if cap['is_full']:
        return CLASS_FULL
    if not seats.trial_open(lesson):
        return CLOSED
    if cap['trial_is_full']:
        return TRIAL_FULL if seats.dates(lesson) else NO_DATES
    return ''


def _lesson_rows(lesson, course, seats: Seats) -> tuple:
    """(picker rows, reason) for a lesson a parent opened by itself."""
    reason = _lesson_gate(lesson, course, seats)
    if reason:
        return [], reason
    return [_slot(lesson, course, day, seats) for day in seats.dates(lesson)], ''


def _track_rows(members: list, course, seats: Seats) -> tuple:
    """
    (picker rows, {lesson id: reason}) for a combined track a parent opened.

    The card of a track knows less than the card of a lesson: it is full when
    one of its days is, and it offers a trial while one of its days is open to
    trials — the seats of a trial are not on it (the catalogue sends a track's
    lessons without them). Its picker then lists the earliest dates of all the
    open days together, three in all, full ones marked.
    """
    from apps.enrollments.trial_reminders import TRIAL_LESSON_OCCURRENCE_LIMIT, iter_merged_upcoming_lesson_occurrences

    if any(seats.cap(member, course)['is_full'] for member in members):
        return [], {member.id: CLASS_FULL if seats.cap(member, course)['is_full'] else TRACK_FULL for member in members}
    reasons = {member.id: CLOSED for member in members if not seats.trial_open(member)}
    choices = [member for member in members if member.id not in reasons]
    if not choices:
        return [], reasons
    if len(choices) == 1:
        return [_slot(choices[0], course, day, seats) for day in seats.dates(choices[0])], reasons
    merged = iter_merged_upcoming_lesson_occurrences(choices, count=TRIAL_LESSON_OCCURRENCE_LIMIT, now=seats.now)
    return [_slot(member, course, day, seats) for member, day in merged], reasons


def _started(slot: Slot, now) -> bool:
    """A lesson of today that has already begun: still on the form, not something to invite a parent to."""
    return slot.day == now.date() and now.time() >= slot.lesson.start_time


def _fell_out(lesson, seats: Seats, now, until: date) -> list:
    """The weekly dates of a lesson from today to `until` that the form skips — a cancelled lesson, a closed day."""
    from apps.enrollments.trial_reminders import next_lesson_occurrence, trial_lesson_min_date

    if not lesson.is_recurring:
        return []
    cancelled, blocked = seats.occurrences
    gone, closed = cancelled.get(lesson.id, set()), blocked.get(lesson.id) or frozenset()
    earliest = trial_lesson_min_date(now=now)
    day = next_lesson_occurrence(lesson.day_of_week, lesson.end_time, now=now)
    found = []
    while day <= until:
        if day >= earliest:
            if day in gone:
                found.append(Skipped(lesson=lesson, day=day, why=SKIP_CANCELLED))
            elif day in closed:
                found.append(Skipped(lesson=lesson, day=day, why=SKIP_BLOCKED, reason=seats.block_reason(day)))
        day += timedelta(days=7)
    return found


def for_course(course, seats: Seats) -> Offer:
    """
    The trial dates of one class as a parent on the site would find them now.
    `course` comes with its lessons and tracks loaded as the catalogue loads
    them (course_tools.with_lessons), and `seats` was built over it.
    """
    now = seats.now or timezone.localtime()
    lessons = {lesson.id: lesson for lesson in course.lessons.all()}
    offer = Offer()

    def settle(reason: str) -> Offer:
        offer.reason = reason
        offer.by_lesson = {lesson_id: reason for lesson_id in lessons}
        return offer

    if not in_catalogue(course):
        return settle(HIDDEN)
    if (course.external_link or '').strip():
        return settle(EXTERNAL_LINK)
    rows = selections(course)
    if not rows:
        return settle(NO_LESSONS)
    if course.trial_lesson_is_paid and not (course.trial_lesson_price is not None and course.trial_lesson_price > 0):
        return settle(NO_PRICE)

    found: dict = {}                # (lesson id, date) → Slot
    said: dict = {}                 # lesson id → what each row of the form that holds it answered
    for lesson, bundle in rows:
        if lesson is not None:
            # The class's own lesson objects: they carry the class and the room already.
            members = [lessons.get(lesson.id, lesson)]
            slots, reason = _lesson_rows(members[0], course, seats)
            reasons = {members[0].id: reason}
        else:
            members = [lessons.get(member.id, member) for member in bundle.lessons.all()]
            slots, reasons = _track_rows(members, course, seats)
        for slot in slots:
            found[(slot.lesson.id, slot.day)] = slot
        for member in members:
            listed = any(slot.lesson.id == member.id for slot in slots)
            said.setdefault(member.id, set()).add(reasons.get(member.id) or ('' if listed else NO_DATES))

    for slot in sorted(found.values(), key=lambda row: (row.day, row.lesson.start_time, str(row.lesson.id))):
        if _started(slot, now):
            offer.skipped.append(Skipped(lesson=slot.lesson, day=slot.day, why=SKIP_STARTED))
        else:
            offer.slots.append(slot)

    for lesson_id in lessons:
        mine = [slot for slot in offer.slots if slot.lesson.id == lesson_id]
        if any(not slot.is_full for slot in mine):
            reason = ''
        elif mine:
            reason = TRIAL_FULL
        elif any(skip.lesson.id == lesson_id for skip in offer.skipped):
            reason = STARTED
        elif lesson_id not in said:
            reason = TRACK_ONLY     # a day of a class whose every day is required, in no track the form shows
        else:
            reasons = said[lesson_id] - {''}
            reason = reasons.pop() if len(reasons) == 1 else (MIXED if reasons else NO_DATES)
        offer.by_lesson[lesson_id] = reason

    # The dates that fell out before the last one offered — or in the coming weeks, when none is.
    for lesson_id, lesson in lessons.items():
        if lesson_id not in said or offer.by_lesson[lesson_id] in (CLOSED, CLASS_FULL, TRACK_FULL):
            continue
        last = max((slot.day for slot in offer.slots if slot.lesson.id == lesson_id), default=None)
        offer.skipped.extend(_fell_out(lesson, seats, now, last or now.date() + timedelta(weeks=SKIPPED_WEEKS)))
    offer.skipped.sort(key=lambda row: (row.day, row.lesson.start_time, str(row.lesson.id)))

    if not offer.can_book:
        reasons = {reason for reason in offer.by_lesson.values() if reason}
        if reasons and reasons <= {CLASS_FULL, TRACK_FULL}:
            reasons = {CLASS_FULL}      # a full day and the days that are full with it: the class is full
        offer.reason = reasons.pop() if len(reasons) == 1 else (MIXED if reasons else NO_DATES)
    return offer
