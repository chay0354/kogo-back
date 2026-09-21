import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_manychat_contact'),
    ]

    operations = [
        migrations.CreateModel(
            name='DailyBriefSnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('payload', models.JSONField(verbose_name='הבריף')),
                ('red_count', models.PositiveIntegerField(default=0, verbose_name='נושאים דחופים')),
                ('yellow_count', models.PositiveIntegerField(default=0, verbose_name='נושאים לבדיקה')),
                ('duration_ms', models.PositiveIntegerField(default=0, verbose_name='זמן חישוב (מילישניות)')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='נוצר בתאריך')),
            ],
            options={
                'verbose_name': 'בריף יומי',
                'verbose_name_plural': 'בריפים יומיים',
                'db_table': 'daily_brief_snapshots',
                'ordering': ['-created_at'],
            },
        ),
    ]
