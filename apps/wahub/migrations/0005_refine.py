# "דיוק" and the automatic approval (docs/WAHUB-CONTRACT-REFINE.md, א): the small
# chat in which the operator says what was wrong (threads and turns), the app's
# own key/value settings (`review_auto_apply`, the place the one-time sweep over
# the old conversations has reached), and on a proposal: that it was applied
# with no click, which version of the item it made, and who took it back.
# Nothing is backfilled and nothing is dropped.
#
# Safe for the code that is already running. A Vercel build of this branch
# migrates the production database while production's code — which does not
# know `auto_applied` — keeps creating proposals from the cron's sweep. Django
# 4.2 adds the column NOT NULL and drops its default again, so those inserts
# would fail; the database keeps the `false` default (as 0004 does for
# `live_test`). The other three columns allow NULL; the two AlterFields change
# `choices` only and touch nothing in the database.
#
# The three new tables get row level security with no policy, owner only, as
# every table of this app has (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


RLS_TABLES = (
    'wahub_settings',
    'wahub_refine_threads',
    'wahub_refine_turns',
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
        ('wahub', '0004_live_test'),
    ]

    operations = [
        migrations.CreateModel(
            name='RefineThread',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message_ids', models.JSONField(blank=True, default=list)),
                ('shadow_ids', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(choices=[('open', 'פתוח'), ('done', 'הסתיים')], default='open', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contact', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='refine_threads', to='wahub.contact')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('proposal', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='refine_threads', to='wahub.knowledgeproposal')),
            ],
            options={
                'verbose_name': 'דיוק',
                'verbose_name_plural': 'דיוקים',
                'db_table': 'wahub_refine_threads',
                'ordering': ['-id'],
            },
        ),
        migrations.AddField(
            model_name='knowledgeproposal',
            name='applied_version',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='knowledgeproposal',
            name='auto_applied',
            field=models.BooleanField(default=False),
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "wahub_knowledge_proposals" ALTER COLUMN "auto_applied" SET DEFAULT false',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name='knowledgeproposal',
            name='undone_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='knowledgeproposal',
            name='undone_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='knowledgeproposal',
            name='source',
            field=models.CharField(choices=[('human_override', 'נציג ענה מעל הבוט'), ('bad_verdict', 'סימון 👎 על תשובת צל'), ('service_note', 'הערת שירות'), ('reviewer', 'סריקה יזומה של השיחות'), ('refine', 'דיוק')], max_length=20),
        ),
        migrations.AlterField(
            model_name='knowledgeproposal',
            name='status',
            field=models.CharField(choices=[('pending', 'ממתין לאישור'), ('approved', 'אושר'), ('rejected', 'נדחה'), ('applied', 'הוחל על הידע'), ('undone', 'בוטל')], default='pending', max_length=10),
        ),
        migrations.CreateModel(
            name='WahubSetting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=64, unique=True)),
                ('value', models.JSONField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'הגדרה של וואטסאפ ולידים',
                'verbose_name_plural': 'הגדרות של וואטסאפ ולידים',
                'db_table': 'wahub_settings',
            },
        ),
        migrations.CreateModel(
            name='RefineTurn',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('operator', 'מפעילה'), ('system', 'המערכת')], max_length=10)),
                ('text', models.TextField(blank=True)),
                ('changes', models.JSONField(blank=True, default=list)),
                ('needs_answer', models.BooleanField(default=False)),
                ('meta', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('thread', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='turns', to='wahub.refinethread')),
            ],
            options={
                'verbose_name': 'הודעה בדיוק',
                'verbose_name_plural': 'הודעות בדיוק',
                'db_table': 'wahub_refine_turns',
                'ordering': ['id'],
                'indexes': [models.Index(fields=['thread', 'id'], name='wahub_refine_turn_thread')],
            },
        ),
        migrations.AddIndex(
            model_name='refinethread',
            index=models.Index(fields=['contact', '-id'], name='wahub_refine_contact'),
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
