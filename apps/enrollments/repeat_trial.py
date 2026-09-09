"""
A second (third…) trial lesson for a child who already had one.

The office books it from the CRM; the widget refuses it and sends the parent
to the office. Trial rows carry `trial_number`, so the register can show a
small "ניסיון 2" tag next to the child.
"""
from django.db.models import Q
from django.utils import timezone

from apps.enrollments.models import LessonEnrollment

REPEAT_TRIAL_WIDGET_ERROR = 'לילד כבר היה שיעור ניסיון. לתיאום ניסיון נוסף אנא פנו למשרד.'


def _trial_history(child):
    """Rows that were a trial at some point: a date still on them, or an outcome the cron wrote."""
    return LessonEnrollment.objects.filter(child=child).filter(
        Q(trial_lesson_date__isnull=False) | ~Q(trial_outcome='')
    )


def child_had_trial(child) -> bool:
    """A trial that already took place, or was booked and its date went by."""
    if child is None:
        return False
    if child.status == 'trial_completed' or (child.trial_classes_attended or 0) > 0:
        return True
    today = timezone.localdate()
    return _trial_history(child).filter(Q(trial_lesson_date__lt=today) | ~Q(trial_outcome='')).exists()


def next_trial_number(child, *, exclude_id=None) -> int:
    """1 for a first trial; one more than the highest number the child's trials carry."""
    rows = _trial_history(child)
    if exclude_id is not None:
        rows = rows.exclude(id=exclude_id)
    highest = max(rows.values_list('trial_number', flat=True), default=0)
    if highest == 0 and (child.status == 'trial_completed' or (child.trial_classes_attended or 0) > 0):
        highest = 1  # the trial row was converted or removed; the counter remembers it
    return highest + 1
