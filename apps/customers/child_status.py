"""
The statuses a child can be in — one list, one meaning, one place.

Before this module there were three disagreeing lists: the model's choices, a
label map in the dashboard that invented `non_active`, `paused` and `sign_in`,
and `calculate_status()`, which returned `expired` and `trial` — values no
choice, serializer or screen ever knew, so a child it touched showed up as
"לא מוגדר".

The six below are the whole set. Anything else read out of the database is
legacy and is mapped on sight by `canonical_status`.

    active           פעיל            money actually came in
    trial_signed     נרשם לניסיון     a trial lesson is booked, still ahead
    trial_completed  ביצע ניסיון      the trial already happened
    pending          בתהליך רישום     details filled in, nothing paid
    payment_problem  בעיה באשראי      the card did not go through
    inactive         לא פעיל         had something, it was cancelled, nothing left
    ghost            רפאים            a walk-in the instructor added

`pending` and `inactive` are easy to confuse and are not the same thing. A
child in בתהליך רישום never finished registering — the details are in, the
payment never happened. A child in לא פעיל did have something and it was
cancelled: they are not paying and they are not in anything any more.

Precedence, when more than one could be argued: money wins. A child who paid is
`active` no matter what else is true of them, which is what makes "פעיל" worth
reading. `ghost` is not really a competing status — a walk-in that turns out to
be a child the system already knows is merged into that child rather than kept
alongside them.
"""
from __future__ import annotations

from datetime import date

STATUS_ACTIVE = 'active'
STATUS_TRIAL_SIGNED = 'trial_signed'
STATUS_TRIAL_COMPLETED = 'trial_completed'
STATUS_PENDING = 'pending'
STATUS_PAYMENT_PROBLEM = 'payment_problem'
STATUS_INACTIVE = 'inactive'
STATUS_GHOST = 'ghost'

CHILD_STATUS_CHOICES = [
    (STATUS_ACTIVE, 'פעיל'),
    (STATUS_TRIAL_SIGNED, 'נרשם לניסיון'),
    (STATUS_TRIAL_COMPLETED, 'ביצע ניסיון'),
    (STATUS_PENDING, 'בתהליך רישום'),
    (STATUS_PAYMENT_PROBLEM, 'בעיה באשראי'),
    (STATUS_INACTIVE, 'לא פעיל'),
    (STATUS_GHOST, 'רפאים'),
]

CHILD_STATUS_LABELS = dict(CHILD_STATUS_CHOICES)
CHILD_STATUSES = [value for value, _ in CHILD_STATUS_CHOICES]

# Lower wins when one child was created twice, or when two facts could each
# justify a status. Money first — that is the whole point of "פעיל".
CHILD_STATUS_RANK = {
    STATUS_ACTIVE: 0,
    STATUS_PAYMENT_PROBLEM: 1,
    STATUS_TRIAL_SIGNED: 2,
    STATUS_TRIAL_COMPLETED: 3,
    STATUS_PENDING: 4,
    STATUS_INACTIVE: 5,
    STATUS_GHOST: 6,
}

# Values written before this list was settled. `not_paid` was never set by any
# code — only read, by a dashboard KPI that counted it beside payment_problem,
# so that is where it lands. The rest said something about a child's state
# rather than a fact about them, and are worked out again from what is recorded.
LEGACY_STATUS_MAP = {
    'not_paid': STATUS_PAYMENT_PROBLEM,
    'non_active': STATUS_INACTIVE,
    'expired': None,      # resolve from the facts
    'trial': None,
    'paused': None,
    'sign_in': None,
}


# An enrolment that still counts the child as being on the lesson.
# 'payments_problem' is a billing flag, not a cancellation — the child is there.
LIVE_ENROLLMENT_STATUSES = ('active', 'payments_problem')


def canonical_status(status: str) -> str | None:
    """
    The canonical name for a stored status, or None when it has to be resolved.

    Unknown values resolve rather than pass through: a status no screen can
    render is worse than one worked out from the child's own record.
    """
    if status in CHILD_STATUS_LABELS:
        return status
    return LEGACY_STATUS_MAP.get(status, None)


def status_label(status: str) -> str:
    return CHILD_STATUS_LABELS.get(status, 'לא מוגדר')


def _has_money_in(child) -> bool:
    """
    Is this child's money in the system?

    Paid up to a date that has not passed, or — for a registration that has
    just gone through and has no such date recorded yet — a completed payment.

    Two readings are deliberately excluded. An enrolment is not money: that was
    the old frontend's rule, and it showed children as פעיל who had never paid
    a shekel. And a completed payment on its own does not last forever: a
    parent who paid two years ago and left would otherwise stay פעיל for good,
    which is exactly what makes the word worth reading.
    """
    today = date.today()
    if child.paid_until_date:
        return child.paid_until_date >= today
    return child.payments.filter(status='completed').exists()


def _trial_dates(child):
    """(has a trial still ahead, has had a trial already)."""
    today = date.today()
    ahead = child.lesson_enrollments.filter(trial_lesson_date__gte=today).exists()
    held = child.lesson_enrollments.filter(trial_held_on__lt=today).exists()
    return ahead, held


def resolve_child_status(child) -> str:
    """
    Work the status out from what is recorded about the child.

    Used when a stored value cannot be trusted — a legacy name, or a moment
    where the code used to hard-code one (a child whose last course was
    removed was written straight to `inactive`, a status that no longer
    exists). A ghost stays a ghost until it is merged into a real child; that
    merge is the walk-in flow's job, not this function's.
    """
    if child.status == STATUS_GHOST:
        return STATUS_GHOST

    if _has_money_in(child):
        return STATUS_ACTIVE

    # The card failed and no money has come in since, so the problem stands.
    if child.status == STATUS_PAYMENT_PROBLEM:
        return STATUS_PAYMENT_PROBLEM

    trial_ahead, trial_held = _trial_dates(child)
    if trial_ahead:
        return STATUS_TRIAL_SIGNED
    if trial_held:
        return STATUS_TRIAL_COMPLETED

    # Nothing paid, no trial. What separates the last two is whether this child
    # had something and lost it. Still on a lesson, just unpaid, is בתהליך
    # רישום — the registration never finished. Every lesson they had now
    # cancelled is לא פעיל: they are not paying and not in anything.
    enrollments = child.lesson_enrollments
    if enrollments.filter(status__in=LIVE_ENROLLMENT_STATUSES).exists():
        return STATUS_PENDING
    if enrollments.exists():
        return STATUS_INACTIVE

    return STATUS_PENDING
