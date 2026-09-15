"""
The two model edits that had no migration of their own.

`0015` and `0017` each found them already outstanding and each left them alone,
saying they belong to the migration that owns them. This is that migration, and
nothing else is in it. Until now every `makemigrations` anyone ran produced this
same pair as a stray file, so `makemigrations --check` could not be used as a
gate on anything.

The SQL is one `ALTER INDEX ... RENAME` on `cron_heartbeat` — metadata only, no
table rewrite — and a `help_text` edit on `payment.registration_fee`, which
Django itself reports as a no-op. No data moves.
"""

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0017_computerized_docs_consent'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='cronheartbeat',
            new_name='cron_heartb_invoked_533153_idx',
            old_name='cron_heartb_invoked_idx',
        ),
        migrations.AlterField(
            model_name='payment',
            name='registration_fee',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='דמי רישום חד-פעמיים — פעם אחת לכל ילד, בחיוב הראשון בלבד', max_digits=10, verbose_name='דמי רישום'),
        ),
    ]
