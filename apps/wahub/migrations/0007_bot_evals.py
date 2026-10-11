# "בדיקות הבוט" (docs/WAHUB-CONTRACT-BOT-EVALS.md): one table, the runs of the bot's
# self-checks — a written conversation played in memory against the bot, with what it
# answered, the tools it called and the verdict of every check. Nothing is backfilled
# and nothing existing is touched.
#
# Safe for the code that is already running: a new table that production's code neither
# reads nor writes. A Vercel build of this branch creates it in the production database
# and nothing else changes there.
#
# Row level security with no policy, owner only, as every table of this app has
# (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


RLS_TABLES = ('wahub_bot_eval_runs',)


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
        ('wahub', '0006_lead_stage'),
    ]

    operations = [
        migrations.CreateModel(
            name='BotEvalRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('scenario_id', models.CharField(max_length=80)),
                ('status', models.CharField(choices=[('running', 'רץ'), ('passed', 'עבר'), ('failed', 'נכשל'), ('skipped', 'דולג'), ('error', 'לא רץ')], default='running', max_length=10)),
                ('engine', models.CharField(blank=True, max_length=80)),
                ('world', models.JSONField(blank=True, default=dict)),
                ('turns', models.JSONField(blank=True, default=list)),
                ('checks', models.JSONField(blank=True, default=list)),
                ('detail', models.TextField(blank=True)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('took_ms', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'בדיקת בוט',
                'verbose_name_plural': 'בדיקות הבוט',
                'db_table': 'wahub_bot_eval_runs',
                'ordering': ['-id'],
                'indexes': [models.Index(fields=['scenario_id', '-id'], name='wahub_eval_scenario')],
            },
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
