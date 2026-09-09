"""
A second (third…) trial lesson for a child who already had one.

The office books it from the CRM; the widget refuses it and sends the parent
to the office. Trial rows carry `trial_number`, so the register can show a
small "ניסיון 2" tag next to the child.
"""
from django.db.models import Q
from django.utils import timezone

from apps.customers.child_identity import normalize_id_number
from apps.customers.models import Child
from apps.enrollments.models import LessonEnrollment

REPEAT_TRIAL_WIDGET_ERROR = 'לילד כבר היה שיעור ניסיון. לתיאום ניסיון נוסף אנא פנו למשרד.'


def _held_or_numbered(child):
    """
    Trials that count as history: the cron wrote an outcome, the date passed
    while the row was still booked, or the row was already reused for a
    repeat (trial_number > 1). A trial dropped before its date — inactive with
    no outcome — never took place and does not count.
    """
    today = timezone.localdate()
    return LessonEnrollment.objects.filter(child=child).filter(
        ~Q(trial_outcome='')
        | Q(trial_lesson_date__lt=today, status='active')
        | Q(trial_number__gt=1)
    )


def _identity_twins(child):
    """The child's own card plus any other card carrying the same id number (another family, a duplicate)."""
    twins = [child]
    id_number = normalize_id_number(getattr(child, 'id_number', '') or '')
    if id_number:
        for other in Child.objects.exclude(id=child.id).filter(id_number__isnull=False).exclude(id_number=''):
            if normalize_id_number(other.id_number) == id_number:
                twins.append(other)
    return twins


def _card_had_trial(card) -> bool:
    if card.status == 'trial_completed' or (card.trial_classes_attended or 0) > 0:
        return True
    return _held_or_numbered(card).exists()


def child_had_trial(child) -> bool:
    """A trial that already took place (or was reused for a repeat) — on this card or a twin of it."""
    if child is None:
        return False
    return any(_card_had_trial(card) for card in _identity_twins(child))


def next_trial_number(child, *, exclude_id=None) -> int:
    """1 for a first trial; one more than the highest number the child's held trials carry."""
    rows = _held_or_numbered(child)
    if exclude_id is not None:
        rows = rows.exclude(id=exclude_id)
    highest = max(rows.values_list('trial_number', flat=True), default=0)
    if highest == 0 and (child.status == 'trial_completed' or (child.trial_classes_attended or 0) > 0):
        highest = 1  # the trial row was converted or removed; the counter remembers it
    return highest + 1
