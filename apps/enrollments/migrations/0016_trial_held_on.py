from django.db import migrations, models
from django.db.models import F


def backfill_trial_held_on(apps, schema_editor):
    """
    Every trial we can still date, dated.

    Rows that still carry `trial_lesson_date` copy it across. The ones that
    already lost it to a conversion cannot be recovered from this table — their
    date survives only in the attendance rows — so they stay null and read as
    "a trial happened, date unknown" rather than having one invented for them.
    """
    LessonEnrollment = apps.get_model('enrollments', 'LessonEnrollment')
    LessonEnrollment.objects.filter(
        trial_lesson_date__isnull=False, trial_held_on__isnull=True,
    ).update(trial_held_on=F('trial_lesson_date'))


def noop(apps, schema_editor):
    """Nothing to undo: the column goes with the field."""


class Migration(migrations.Migration):

    dependencies = [
        ('enrollments', '0015_lesson_enrollment_trial_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='lessonenrollment',
            name='trial_held_on',
            field=models.DateField(
                blank=True, editable=False, null=True,
                verbose_name='תאריך הניסיון (היסטורי)',
                help_text='נשמר גם אחרי שהילד נרשם, כדי שאפשר יהיה לראות מי היה בניסיון ומתי.',
            ),
        ),
        migrations.RunPython(backfill_trial_held_on, noop),
    ]
