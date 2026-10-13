# "הסר" — the phones that asked not to get broadcast messages on WhatsApp
# (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.1): one new table, `whatsapp_opt_outs`, read by every
# broadcast before its first message (apps/core/whatsapp_optout.py). An addition only: nothing is
# backfilled, renamed or dropped, and no existing table gets a column.
#
# Safe for the code that is already running. A Vercel build of this branch migrates the production
# database while production's code keeps running: that code does not know the table and never
# touches it. The two foreign keys point at the users' table; adding them asks for a moment's lock
# on it, so the lock is asked for five seconds and no longer (as apps/wahub and apps/rentals do) —
# a build that cannot get it fails, changes nothing, and is simply run again.
#
# The table holds phones, so it gets row level security with no policy, owner only — nobody but
# the application reads a row (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md), as every table of the
# WhatsApp section has.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


RLS_TABLES = (
    'whatsapp_opt_outs',
)


def enable_row_level_security(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for table in RLS_TABLES:
        schema_editor.execute(f'ALTER TABLE {schema_editor.quote_name(table)} ENABLE ROW LEVEL SECURITY')


def disable_row_level_security(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for table in RLS_TABLES:
        schema_editor.execute(f'ALTER TABLE {schema_editor.quote_name(table)} DISABLE ROW LEVEL SECURITY')


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0031_branches_business'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.CreateModel(
            name='WhatsAppOptOut',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('phone', models.CharField(max_length=20, verbose_name='טלפון (בינלאומי)')),
                ('opted_out_at', models.DateTimeField(verbose_name='ביקש הסר')),
                ('source', models.CharField(choices=[('message', 'ביקש בהודעה'), ('office', 'סומן על ידי נציג')], max_length=10, verbose_name='מאיפה')),
                ('text', models.CharField(blank=True, max_length=300, verbose_name='ההודעה')),
                ('wahub_contact_id', models.BigIntegerField(blank=True, null=True)),
                ('wahub_message_id', models.BigIntegerField(blank=True, null=True)),
                ('restored_at', models.DateTimeField(blank=True, null=True, verbose_name='הוחזר לתפוצה')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('opted_out_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='סומן על ידי')),
                ('restored_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='הוחזר על ידי')),
            ],
            options={
                'verbose_name': 'ביקש הסר',
                'verbose_name_plural': 'ביקשו הסר',
                'db_table': 'whatsapp_opt_outs',
                'ordering': ['-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='whatsappoptout',
            constraint=models.UniqueConstraint(condition=models.Q(('restored_at__isnull', True)), fields=('phone',), name='whatsapp_opt_out_one_standing'),
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
