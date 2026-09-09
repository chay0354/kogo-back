from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('enrollments', '0014_trial_registration_policy'),
    ]

    operations = [
        migrations.AddField(
            model_name='lessonenrollment',
            name='trial_number',
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text='1 = ניסיון ראשון; ניסיון נוסף שנרשם מהמערכת מקבל 2, 3…',
                verbose_name='מספר הניסיון',
            ),
        ),
    ]
