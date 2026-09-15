"""
An instructor's trial students, gathered across their lessons.

Until now a trial child could be found in exactly one place: the register for
their own lesson on their own date. The day after, they were gone. An
instructor who wanted to ring the parents of last week's trials had to remember
each date and walk back to it, one lesson at a time — so in practice nobody
called anyone.

Four things are worth knowing about how the data behaves, because all four
shaped this:

* **The outcome is on the enrolment, not the child.** ``Child.status`` only ever
  says whether the date went by (``trial_signed`` → ``trial_completed``); what
  actually happened is ``LessonEnrollment.trial_outcome``.
* **``unmarked`` is not ``no_show``.** The register simply was not taken. Folding
  the two together would tell an instructor a child did not turn up when nobody
  knows either way, and that is a phone call made on a false premise.
* **A conversion erases ``trial_lesson_date``.** It has to — see the note on
  ``trial_held_on`` — so the trials that *worked* were the ones that vanished.
  Everything here reads ``trial_held_on``, which survives.
* **A child who converted has ``Child.status = 'active'``.** They are no longer a
  trial by status, but they are exactly who an instructor wants to see: the
  proof the class sells itself. They appear here as ``registered``.
"""
from __future__ import annotations

from datetime import date

from django.db.models import Q

from apps.customers.models import RecurringPayment
from apps.enrollments.models import LessonEnrollment

# What a row is reported as. Deliberately five, not three: 'unmarked' keeps the
# honest gap between "did not come" and "nobody took the register", and
# 'registered' is the trial that turned into a paying student.
OUTCOME_UPCOMING = 'upcoming'
OUTCOME_ATTENDED = 'attended'
OUTCOME_NO_SHOW = 'no_show'
OUTCOME_UNMARKED = 'unmarked'
OUTCOME_REGISTERED = 'registered'

OUTCOME_LABELS = {
    OUTCOME_UPCOMING: 'נרשם לניסיון',
    OUTCOME_ATTENDED: 'הגיע',
    OUTCOME_NO_SHOW: 'לא הגיע',
    OUTCOME_UNMARKED: 'לא סומן',
    OUTCOME_REGISTERED: 'נרשם בסוף',
}


def _phone(child) -> str:
    """The child's own number, falling back to the family's — as the register does."""
    from apps.scheduling.views import child_contact_phone

    return child_contact_phone(child)


def _paying_lesson_ids(child_ids) -> set:
    """
    Which (child, lesson) pairs are paying today.

    A trial counts as converted when the child holds a live standing order for
    the lesson they trialled at — the same test the trial cron uses before it
    retires a row, so the two can never disagree about who subscribed.
    """
    pairs = set()
    if not child_ids:
        return pairs
    rows = (
        RecurringPayment.objects
        .filter(child_id__in=child_ids, status__in=('active', 'paused'))
        .values_list('child_id', 'initial_payment__lesson_id', 'initial_payment__bundle__lessons__id')
    )
    for child_id, lesson_id, bundle_lesson_id in rows:
        if lesson_id:
            pairs.add((child_id, lesson_id))
        if bundle_lesson_id:
            pairs.add((child_id, bundle_lesson_id))
    return pairs


def _outcome_for(row, paying, today: date) -> str:
    held = row.trial_held_on or row.trial_lesson_date
    if held and held > today:
        return OUTCOME_UPCOMING
    if (row.child_id, row.lesson_id) in paying:
        return OUTCOME_REGISTERED
    if row.trial_outcome == 'attended':
        return OUTCOME_ATTENDED
    if row.trial_outcome == 'no_show':
        return OUTCOME_NO_SHOW
    return OUTCOME_UNMARKED


def trials_for_lessons(lesson_ids, date_from: date, date_to: date, today: date | None = None) -> dict:
    """
    Every trial held on those lessons in that window, with what became of it.

    Returns ``{'counts': {...}, 'trials': [...]}``. One row per trial, newest
    first, each carrying the phone the instructor would call.
    """
    today = today or date.today()
    if not lesson_ids:
        return {'counts': {k: 0 for k in OUTCOME_LABELS}, 'total': 0, 'trials': []}

    rows = list(
        LessonEnrollment.objects
        .filter(lesson_id__in=lesson_ids)
        .filter(
            Q(trial_held_on__gte=date_from, trial_held_on__lte=date_to)
            # Rows written before trial_held_on existed and never touched since.
            | Q(trial_held_on__isnull=True, trial_lesson_date__gte=date_from,
                trial_lesson_date__lte=date_to)
        )
        .select_related('child', 'child__family', 'lesson', 'lesson__course')
        .order_by('-trial_held_on', '-trial_lesson_date')
    )

    paying = _paying_lesson_ids({row.child_id for row in rows})
    counts = {key: 0 for key in OUTCOME_LABELS}
    trials = []
    for row in rows:
        outcome = _outcome_for(row, paying, today)
        counts[outcome] += 1
        held = row.trial_held_on or row.trial_lesson_date
        trials.append({
            'enrollment_id': str(row.id),
            'child_id': str(row.child_id),
            'child_name': row.child.full_name,
            'phone': _phone(row.child),
            'lesson_id': str(row.lesson_id),
            'course_name': row.lesson.course.name if row.lesson.course_id else '',
            'day_of_week': row.lesson.day_of_week,
            'start_time': row.lesson.start_time.strftime('%H:%M') if row.lesson.start_time else '',
            'trial_date': held.isoformat() if held else None,
            'trial_number': row.trial_number,
            'outcome': outcome,
            'outcome_label': OUTCOME_LABELS[outcome],
            'child_status': row.child.status,
        })

    return {'counts': counts, 'total': len(trials), 'trials': trials}
