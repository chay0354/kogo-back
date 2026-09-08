"""Whether a lesson offers a trial — the rule, and each lesson's own answer.

Every place that decides is meant to ask here: the widget payload that shows
or hides the button, the submit check that refuses a booking the button was
hidden for, and the settings screen that lists what is open where. One
function, so the three cannot disagree.
"""
from __future__ import annotations

from apps.enrollments.models import TrialRegistrationPolicy


def trials_open_by_default() -> bool:
    return TrialRegistrationPolicy.current().trials_open


def trial_registration_open_for(lesson, *, default: bool | None = None) -> bool:
    """
    A lesson's own setting when it has one, the studio rule otherwise.

    Pass `default` when answering for many lessons at once, so the rule is
    read once rather than per row.
    """
    if lesson.trial_registration_open is not None:
        return bool(lesson.trial_registration_open)
    return trials_open_by_default() if default is None else default
