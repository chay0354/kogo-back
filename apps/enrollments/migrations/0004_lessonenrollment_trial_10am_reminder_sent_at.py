from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('enrollments', '0003_lessonenrollment_trial_evening_reminder_sent_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='lessonenrollment',
            name='trial_10am_reminder_sent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='תזכורת 10:00 ביום הניסיון נשלחה'),
        ),
    ]
