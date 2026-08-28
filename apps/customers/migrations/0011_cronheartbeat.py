from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0010_payment_price_option'),
    ]

    operations = [
        migrations.CreateModel(
            name='CronHeartbeat',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('invoked_at', models.DateTimeField(auto_now_add=True)),
                ('user_agent', models.CharField(blank=True, max_length=300)),
                ('schedule_header', models.CharField(blank=True, max_length=80)),
                ('dry_run', models.BooleanField(default=False)),
                ('is_vercel_cron', models.BooleanField(default=False)),
                ('summary', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'db_table': 'cron_heartbeats',
                'ordering': ['-invoked_at'],
            },
        ),
        migrations.AddIndex(
            model_name='cronheartbeat',
            index=models.Index(fields=['-invoked_at'], name='cron_heartb_invoked_idx'),
        ),
    ]
