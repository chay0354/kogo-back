"""
"מחכים לברכה ליום הולדת" (the owner, 15.10.2026): since when a customer waits for Kogo's birthday greeting
(apps/wahub/birthday.py). One nullable column with no default — PostgreSQL adds it without rewriting the table,
and no row has to be filled: nobody waits until the first tag or message after the deploy.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0016_merge_history_read_walk_in'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name='contact',
            name='birthday_wait_since',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
