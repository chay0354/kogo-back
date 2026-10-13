"""
A child who cancelled and still comes to the end of the month.

Owner, 13.10.2026: "ילד שביטל ועדיין אמור להגיע לסוף החודש — גם הוא רשום. אלא אם
כן בוצע לו זיכוי באותו היום על כל החודש, ואז הוא לא אמור להגיע יותר, ולכן הוא לא
אמור להיות רשום."

Cancelling a class ends the child's place on the spot (change_course.
drop_course_unit sets the row inactive and dates its end to that day), and the
register read only live places — so the child left the instructor's list the
moment the office pressed cancel, with half a paid month still ahead.

Nothing about cancelling changes here: the place is closed, the standing order
stopped, the seat free for the next registration. This only reads. A closed
regular place is still on the register for the rest of the month it was closed
in, while the charge that paid for that month of this class stands. A charge
that was given back does not stand, and the child is off the register again.

A family that pays in cash or by cheque has no charge to read, so their child
leaves the register on the day of the cancellation, as before.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date


def month_of(day: date) -> date:
    return day.replace(day=1)


def leaving_enrollments(lesson_ids, first_day: date, last_day: date) -> dict:
    """
    {lesson_id: [(enrollment, month)]} — the places closed during the months
    that [first_day, last_day] touches whose month is paid for and not refunded.
    `month` is the first day of the month the child still comes in.

    Three queries whatever the range (the charges, and their tracks), and one
    when nobody cancelled.
    """
    from apps.customers.child_status import _bought_a_month, billing_month
    from apps.customers.models import Payment
    from apps.enrollments.models import LessonEnrollment

    lesson_ids = list(lesson_ids)
    if not lesson_ids:
        return {}
    # Whole months: a place closed on the 28th is still on the register on the 3rd.
    first_month = month_of(first_day)
    last_month_end = last_day.replace(day=calendar.monthrange(last_day.year, last_day.month)[1])
    closed = list(
        LessonEnrollment.objects
        .filter(
            lesson_id__in=lesson_ids,
            status='inactive',
            trial_lesson_date__isnull=True,
            trial_outcome='',
            end_date__gte=first_month,
            end_date__lte=last_month_end,
        )
        .select_related('child', 'child__family')
    )
    if not closed:
        return {}

    paid = set()
    payments = (
        Payment.objects
        .filter(
            child_id__in={row.child_id for row in closed},
            payment_type='recurring_subscription',
            status='completed',
            lesson__isnull=False,
        )
        .select_related('bundle')
        .prefetch_related('bundle__lessons')
    )
    for payment in payments:
        if not _bought_a_month(
            payment.payment_type, payment.lesson_id, payment.trial_lesson_date,
            payment.final_amount, payment.registration_fee, payment.trial_credit_amount,
        ):
            continue
        month = billing_month(payment.description, payment.payment_date or payment.created_at)
        covered = {payment.lesson_id}
        if payment.bundle_id:
            covered.update(lesson.id for lesson in payment.bundle.lessons.all())
        for lesson_id in covered:
            paid.add((payment.child_id, lesson_id, month))

    found = defaultdict(list)
    for row in closed:
        month = month_of(row.end_date)
        if (row.child_id, row.lesson_id, month) in paid:
            found[row.lesson_id].append((row, month))
    return dict(found)


def leaving_on(leaving_for_lesson, occurrence_date: date | None) -> list:
    """The rows of one lesson still on the register on that date."""
    if not occurrence_date or not leaving_for_lesson:
        return []
    month = month_of(occurrence_date)
    return [row for row, row_month in leaving_for_lesson if row_month == month]
