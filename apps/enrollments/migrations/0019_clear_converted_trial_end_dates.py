"""
Paying rows that still carry the end date of the trial they grew out of.

The trial cron closes a trial row with end_date = the trial day. When the parent
then paid, enroll_child_in_paid_lessons made that same row active again and left
the end date in place, so a subscription "ended" on the day it began. The
register ignores end_date and showed the child as usual; the instructor's
dashboard, the salary tiers and the monthly snapshots all read it, and dropped
the child from every month after the trial. On 23.9.2026 that was 92 rows for 82
children, every one of them on a live standing order.

Only rows carrying the whole trial signature are cleared:

* the row is active,
* its end date has passed and equals its start date — the trial day, since the
  trial booking wrote both,
* it records a trial outcome, and
* the child holds an active or paused standing order.

A row with an end date for any other reason — a cancellation, a course change —
does not look like this and is left for a person. On the day this was written
one such row existed and is not touched.
"""
from datetime import date

from django.db import migrations
from django.db.models import F


def clear(apps, schema_editor):
    LessonEnrollment = apps.get_model('enrollments', 'LessonEnrollment')
    RecurringPayment = apps.get_model('customers', 'RecurringPayment')

    paying_children = RecurringPayment.objects.filter(
        status__in=('active', 'paused'),
    ).values('child_id')

    (
        LessonEnrollment.objects
        .filter(
            status='active',
            end_date__lt=date.today(),
            end_date=F('start_date'),
            child_id__in=paying_children,
        )
        .exclude(trial_outcome='')
        .update(end_date=None)
    )


class Migration(migrations.Migration):

    dependencies = [
        ('enrollments', '0018_trial_blocked_date_lessons'),
        ('customers', '0021_child_status_add_inactive'),
    ]

    # Forward-only: the dates cleared were wrong, and nothing records which
    # rows held them, so there is nothing true to put back.
    operations = [
        migrations.RunPython(clear, migrations.RunPython.noop),
    ]
