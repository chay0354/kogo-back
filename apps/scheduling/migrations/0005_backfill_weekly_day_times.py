from django.db import migrations


def backfill_weekly_day_times(apps, schema_editor):
    """Copy each weekly studio-rental event's single start_time/end_time into
    weekly_day_times for every day already in weekly_repeat_days, so existing
    rentals keep their schedule once per-day times exist."""
    ScheduleEvent = apps.get_model('scheduling', 'ScheduleEvent')
    qs = ScheduleEvent.objects.filter(
        event_type='weekly',
        is_studio_rental=True,
    ).exclude(start_time__isnull=True).exclude(end_time__isnull=True)

    for event in qs:
        if event.weekly_day_times:
            continue
        days = event.weekly_repeat_days or []
        if not days:
            continue
        event.weekly_day_times = {
            str(int(d)): {
                'start_time': event.start_time.isoformat(),
                'end_time': event.end_time.isoformat(),
            }
            for d in days
        }
        event.save(update_fields=['weekly_day_times'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0004_scheduleevent_weekly_day_times'),
    ]

    operations = [
        migrations.RunPython(backfill_weekly_day_times, noop_reverse),
    ]
