from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('enrollments', '0020_walk_in_last_lesson'),
    ]

    operations = [
        migrations.AddField(
            model_name='lessonenrollment',
            name='trial_bring_friend_sent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='חבר מביא חבר נשלח'),
        ),
    ]
