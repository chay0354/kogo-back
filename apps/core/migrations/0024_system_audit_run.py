from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_daily_brief_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='SystemAuditRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField(unique=True, verbose_name='יום')),
                ('area', models.CharField(max_length=32, verbose_name='אזור')),
                ('total_routes', models.PositiveIntegerField(default=0, verbose_name='נתיבים לבדיקה')),
                ('next_index', models.PositiveIntegerField(default=0, verbose_name='התקדמות')),
                ('called', models.PositiveIntegerField(default=0, verbose_name='נתיבים שנקראו')),
                ('failures', models.JSONField(blank=True, default=list, verbose_name='שגיאות')),
                ('slow', models.JSONField(blank=True, default=list, verbose_name='מסכים איטיים')),
                ('skipped', models.JSONField(blank=True, default=list, verbose_name='דולגו')),
                ('probes', models.JSONField(blank=True, default=list, verbose_name='בדיקות האזור')),
                ('probes_done', models.BooleanField(default=False, verbose_name='בדיקות האזור הסתיימו')),
                ('lease_until', models.DateTimeField(blank=True, null=True, verbose_name='תפוס עד')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='הסתיים')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='התחיל')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='עודכן')),
            ],
            options={
                'verbose_name': 'בדיקת עומק יומית',
                'verbose_name_plural': 'בדיקות עומק יומיות',
                'db_table': 'system_audit_runs',
                'ordering': ['-day'],
            },
        ),
    ]
