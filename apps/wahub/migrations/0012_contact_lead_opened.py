# "No lead is lost in silence" (docs/WAHUB-CONTRACT-LEADS-NO-ONE-LOST.md, ד): one column on the
# contact — `lead_opened`, what the daily look for a lead with nothing to offer found, and when it last
# looked. An addition only: nothing is backfilled, renamed or dropped, and every contact starts at {}.
#
# Safe for the code that is already running, the way 0004–0011 are. A Vercel build of this branch
# migrates the production database while production's code — which does not know the new column —
# keeps creating a contact for every new number that writes. Django 4.2 adds a NOT NULL column and
# drops its default again (it has no db_default), so those inserts would fail; the database keeps
# the default of `lead_opened`.
#
# `wahub_contacts` is written all day. The column needs a moment's lock on it, held until this
# migration commits; if something else holds the table, waiting behind it would queue every query on
# it. So the lock is asked for five seconds and no longer (as 0010 and 0011 do): a build that cannot
# get it fails, changes nothing, and is simply run again.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0011_opt_out_overdue_unknowns'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name='contact',
            name='lead_opened',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "wahub_contacts" ALTER COLUMN "lead_opened" SET DEFAULT \'{}\'::jsonb',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
