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

# Owner, 30.9.2026: a status changed by hand says why. Its history row starts
# with this, and the morning fix leaves a status set this way for a person.
MANUAL_REASON_PREFIX = 'שינוי ידני'

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

# The statuses a trial booking may turn into נרשם לניסיון (owner, 27.9.2026).
# A child on פעיל or בעיה באשראי is already a student: a trial in another course
# says nothing about the course they pay for, and writing trial_signed over them
# took five paying children off their own course's register (a trial_signed
# child's regular row is hidden there) until the office put them back. A ghost
# is merged into a real child, never promoted by a booking. A child created by
# the booking itself starts as pending, so a first booking is covered.
STATUSES_A_TRIAL_MAY_MARK = (
    STATUS_PENDING,
    STATUS_INACTIVE,
    STATUS_TRIAL_COMPLETED,
    STATUS_TRIAL_SIGNED,
)


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

    Paid up to a date that has not passed; a cash or cheque plan the office
    registered and that is still running; or — for a registration that has just
    gone through and has no such date recorded yet — a completed payment for a
    course. A paid trial is not one.

    Two readings are deliberately excluded. An enrolment is not money: that was
    the old frontend's rule, and it showed children as פעיל who had never paid
    a shekel. And a completed payment on its own does not last forever: a
    parent who paid two years ago and left would otherwise stay פעיל for good,
    which is exactly what makes the word worth reading.
    """
    today = date.today()
    if child.paid_until_date and child.paid_until_date >= today:
        return True
    # Cash and cheques never touch paid_until_date: the office takes the money
    # up front and the documents follow month by month. A plan still running
    # is money in — without this, two children paying by cheque sat on
    # בתהליך רישום in production. Asked after the date, so the card-paying
    # majority costs no extra query.
    if child.cash_plans.filter(status='active').exists() or child.check_plans.filter(status='active').exists():
        return True
    if _plan_covers_this_month(child, today):
        return True
    if child.paid_until_date:
        return False
    return _registration_paid(child)


def _plan_covers_this_month(child, today: date) -> bool:
    """
    A cash or cheque plan that finished but paid for the month we are in.

    A plan turns 'completed' when the document for its last month is issued —
    on the 1st of that month — while that month is still paid for. Owner,
    28.9.2026: it counts until the month ends. A cancelled plan never does.
    """
    month_start = today.replace(day=1)
    return (
        child.cash_plans.filter(status='completed', months__due_date__gte=month_start).exists()
        or child.check_plans.filter(status='completed', items__due_date__gte=month_start).exists()
    )


def _registration_paid(child) -> bool:
    """
    A registration whose paid-until date is not recorded yet: is it paid for?

    Only money for a course counts (owner, 27.9.2026). A registration fee on
    its own bought no month: four children read פעיל on their דמי רישום while
    the September charge that was to follow was never collected. So:

      * course money — a payment for a lesson that is more than its fee — counts;
      * a fee-only payment counts only while a standing order with a card is
        still alive to bill the first month (a sign-up whose billing starts
        later is a student from the day they sign);
      * a one-time payment with no lesson never counts.

    Only a registration counts at all. A paid trial is money too, but it buys a
    trial — the parent booked one lesson to see — and a child on it is
    נרשם לניסיון, not פעיל. Payment.trial_lesson_date is what marks one: it is
    set on the trial's payment and nowhere else. In production that was 50 of
    the 55 children this rule was about to promote.
    """
    from django.db.models import F

    registrations = child.payments.filter(
        status='completed', trial_lesson_date__isnull=True, lesson__isnull=False,
    )
    # The same line payment_is_fee_only draws: a paid-trial credit lowered what
    # the card was charged, not the month bought, so it is added back.
    if registrations.filter(final_amount__gt=F('registration_fee') - F('trial_credit_amount')).exists():
        return True
    if not registrations.exists():
        return False
    return child.recurring_payments.filter(status='active').exclude(tranzila_token='').exists()


def _card_failed_on_a_course(child) -> bool:
    """
    A standing order whose charge was declined, on a child still in a course.

    The owner's rule (24.9 and 27.9.2026): a card that failed is still a
    student, labelled בעיה באשראי — not a sign-up that never finished. Only a
    regular place counts; a trial row is not a course they are paying for.
    """
    if not child.recurring_payments.filter(status='failed').exists():
        return False
    return child.lesson_enrollments.filter(
        status__in=LIVE_ENROLLMENT_STATUSES, trial_lesson_date__isnull=True,
    ).exists()


def _trial_dates(child, *, held_after=None):
    """(has a trial still ahead, has had a trial already — after `held_after`, when given)."""
    from django.db.models import F, Q

    today = date.today()
    # Only a live row is a trial booked. Cancelling a trial marks its row
    # inactive and leaves the date on it; reading the date alone kept seven
    # children on נרשם לניסיון after the office had cancelled their trial.
    ahead = child.lesson_enrollments.filter(status='active', trial_lesson_date__gte=today).exists()
    # A trial dropped before its date never took place — the same reading
    # repeat_trial uses. The cancel path ends the row on the day it is dropped
    # and writes no outcome; the cron that retires a trial that did happen
    # ends it on the trial's own date and records what became of it.
    held_rows = child.lesson_enrollments.filter(trial_held_on__lt=today)
    if held_after:
        held_rows = held_rows.filter(trial_held_on__gt=held_after)
    held = (
        held_rows
        .exclude(
            Q(status='inactive') & Q(trial_outcome='')
            & Q(end_date__isnull=False) & Q(end_date__lt=F('trial_held_on'))
        )
        .exists()
    )
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

    # Money ran out. What that means depends on whether they left first.
    #
    #   cancelled, then the paid period ended   -> inactive
    #   never cancelled, the money just stopped -> payment_problem
    #
    # A child who cancels keeps active until the date they paid up to, and
    # turns inactive the moment it passes. One still on a lesson with nothing
    # paid is not a quiet ex-customer: somebody has to chase the payment.
    if child.paid_until_date and child.paid_until_date < date.today():
        # Only a regular place counts as still in the class — the same line
        # _card_failed_on_a_course draws. A former student who books a trial
        # has a live row too, and reading it as a course went unpaid turned
        # them into בעיה באשראי (30.9.2026).
        if child.lesson_enrollments.filter(
            status__in=LIVE_ENROLLMENT_STATUSES, trial_lesson_date__isnull=True,
        ).exists():
            return STATUS_PAYMENT_PROBLEM
        # A trial booked ahead means they are back, whatever they were before;
        # one held after the paid period ended is the trial they came back for.
        trial_ahead, held_since = _trial_dates(child, held_after=child.paid_until_date)
        if trial_ahead:
            return STATUS_TRIAL_SIGNED
        if held_since:
            return STATUS_TRIAL_COMPLETED
        return STATUS_INACTIVE

    # The card failed and no money has come in since, so the problem stands.
    if child.status == STATUS_PAYMENT_PROBLEM:
        return STATUS_PAYMENT_PROBLEM

    # The same fact read off the standing order, for a child whose status says
    # otherwise — typically a registration fee that was taken and a first
    # monthly charge that was then declined.
    if _card_failed_on_a_course(child):
        return STATUS_PAYMENT_PROBLEM

    trial_ahead, trial_held = _trial_dates(child)
    if trial_ahead:
        # A trial booked ahead means they are back, whatever they were before.
        return STATUS_TRIAL_SIGNED

    # Someone recorded this child as cancelled, and nothing since says
    # otherwise. That is a fact about them, not a guess to be re-derived —
    # and cancelling often takes the enrolment rows with it, leaving a child
    # who "was not in anything", which is exactly what לא פעיל describes.
    # Without this the record was overruled by its own absence of evidence.
    if child.status == STATUS_INACTIVE:
        return STATUS_INACTIVE

    if trial_held:
        return STATUS_TRIAL_COMPLETED

    # Nothing paid, no trial, nothing recorded as cancelled. What separates the
    # last two is whether this child had something and lost it: still on a
    # lesson, just unpaid, is בתהליך רישום — the registration never finished.
    # Every lesson they had now cancelled is לא פעיל.
    enrollments = child.lesson_enrollments
    if enrollments.filter(status__in=LIVE_ENROLLMENT_STATUSES).exists():
        return STATUS_PENDING
    if enrollments.exists():
        return STATUS_INACTIVE

    return STATUS_PENDING


def still_charged_child_ids() -> set:
    """
    Children someone is still collecting money from: a live standing order with
    a card behind it, or a cash or cheque plan the office is still running.

    Anything that moves children in bulk must never move one of these to
    לא פעיל or בעיה באשראי. paid_until_date lags billing — it moves only when a
    charge lands — so on the first days of a month a perfectly good subscriber
    reads as if their money ran out.
    """
    from apps.customers.models import RecurringPayment
    from apps.documents.models import CashPlan, CheckPlan

    ids = set(
        RecurringPayment.objects.filter(status='active').exclude(tranzila_token='')
        .values_list('child_id', flat=True)
    )
    ids |= set(CashPlan.objects.filter(status='active').values_list('child_id', flat=True))
    ids |= set(CheckPlan.objects.filter(status='active').values_list('child_id', flat=True))
    # A plan finished this month still paid for this month (_plan_covers_this_month).
    month_start = date.today().replace(day=1)
    ids |= set(
        CashPlan.objects.filter(status='completed', months__due_date__gte=month_start)
        .values_list('child_id', flat=True)
    )
    ids |= set(
        CheckPlan.objects.filter(status='completed', items__due_date__gte=month_start)
        .values_list('child_id', flat=True)
    )
    return ids


def mark_trial_signed(child_id) -> bool:
    """
    A trial was just booked: mark the child נרשם לניסיון, unless they are more than that.

    Only from STATUSES_A_TRIAL_MAY_MARK — a student who books a trial in another
    course stays פעיל / בעיה באשראי. One conditional UPDATE, so a payment landing
    at the same moment cannot be overwritten by a read taken before it.
    Around the model, like the writes it replaced: no signal fires, and the
    caller marks the child's groups for a recount. True when it wrote.
    """
    from apps.customers.models import Child

    return bool(
        Child.objects.filter(pk=child_id, status__in=STATUSES_A_TRIAL_MAY_MARK)
        .update(status=STATUS_TRIAL_SIGNED)
    )


def recheck_after_money_stopped(child, *, reason: str, changed_by=None) -> None:
    """
    Money for this child just stopped — a standing order cancelled, a cheque
    plan cancelled, a payment refunded — so work the status out now.

    Owner, 30.9.2026: at that moment, not the next morning. The status is a
    consequence of the money, never a condition of it: a failure here is
    logged and the cancellation or refund stands.
    """
    import logging

    if child is None or child.status == STATUS_GHOST:
        return
    try:
        refresh_child_status(child, reason=reason, changed_by=changed_by)
    except Exception:
        logging.getLogger(__name__).exception('Status recheck after "%s" failed for child %s', reason, child.pk)


def refresh_child_status(child, *, reason: str, changed_by=None) -> str:
    """
    Work the child's status out again, and save it with a history line if it moved.

    The same steps the morning fix takes for each child it moves — the row
    locked and re-read, the rule applied, the change written to
    ChildStatusHistory with why — for a moment that has just changed the
    record, like the office registering a cash or cheque plan. Returns the
    status the child holds afterwards.

    Most callers add money, so the move is up to פעיל;
    recheck_after_money_stopped is the other way. Either way the one history
    line is this one — the post_save signal stands aside for it.
    """
    from django.db import transaction

    from apps.customers.models import Child
    from apps.customers.status_history_models import ChildStatusHistory

    with transaction.atomic():
        locked = Child.objects.select_for_update().get(pk=child.pk)
        was = locked.status
        target = resolve_child_status(locked)
        if not target or target == was:
            return was
        locked.status = target
        locked._status_history_written = True  # the row below, with its reason
        locked.save(update_fields=['status', 'updated_at'])
        ChildStatusHistory.objects.create(
            child=locked, previous_status=was, new_status=target,
            reason=f'{reason}: {status_label(was)} ← {status_label(target)}',
            changed_by=changed_by if getattr(changed_by, 'is_authenticated', False) else None,
        )
    child.status = target
    return target
