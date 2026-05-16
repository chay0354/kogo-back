# Adds weekly_repeat_days to Django state and syncs DB (column may already exist from older branches).

from django.db import migrations, models


def sync_weekly_repeat_days_column(apps, schema_editor):
    connection = schema_editor.connection
    vendor = connection.vendor
    with connection.cursor() as cursor:
        if vendor == 'postgresql':
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'schedule_events'
                  AND column_name = 'weekly_repeat_days'
                """
            )
            exists = cursor.fetchone()
            if not exists:
                cursor.execute(
                    """
                    ALTER TABLE schedule_events
                    ADD COLUMN weekly_repeat_days jsonb NOT NULL DEFAULT '[]'::jsonb
                    """
                )
            else:
                cursor.execute(
                    """
                    UPDATE schedule_events
                    SET weekly_repeat_days = '[]'::jsonb
                    WHERE weekly_repeat_days IS NULL
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE schedule_events
                    ALTER COLUMN weekly_repeat_days SET DEFAULT '[]'::jsonb
                    """
                )
                try:
                    cursor.execute(
                        """
                        ALTER TABLE schedule_events
                        ALTER COLUMN weekly_repeat_days SET NOT NULL
                        """
                    )
                except Exception:
                    pass
        elif vendor == 'sqlite':
            cursor.execute("PRAGMA table_info(schedule_events)")
            cols = [row[1] for row in cursor.fetchall()]
            if 'weekly_repeat_days' not in cols:
                cursor.execute(
                    """
                    ALTER TABLE schedule_events
                    ADD COLUMN weekly_repeat_days text NOT NULL DEFAULT '[]'
                    """
                )
            else:
                cursor.execute(
                    """
                    UPDATE schedule_events
                    SET weekly_repeat_days = '[]'
                    WHERE weekly_repeat_days IS NULL
                    """
                )


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0002_scheduleevent_studio_rental'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(sync_weekly_repeat_days_column, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='scheduleevent',
                    name='weekly_repeat_days',
                    field=models.JSONField(
                        blank=True,
                        default=list,
                        verbose_name='ימי חזרה שבועית',
                    ),
                ),
            ],
        ),
    ]
