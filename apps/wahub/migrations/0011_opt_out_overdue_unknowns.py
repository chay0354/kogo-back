# "הסר", and "what the bot did not know this week" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט): one
# column on the contact — `opted_out`, the copy of its row on the system's opt-out list that the
# lists, the lead stage and the screens read — and two small new tables: the single answers in
# which the new bot did not know (`wahub_bot_unknowns`) and the topics they are gathered under
# (`wahub_bot_unknown_topics`). Additions only: nothing is backfilled, renamed or dropped. The
# AlterField changes `choices` alone (a new kind of journal line, "הסר מתפוצה") and touches nothing
# in the database. The conversation that waits for a person more than three office hours (ט.2) needs
# no column at all: it is read from what the contact already carries.
#
# Safe for the code that is already running, the way 0004–0010 are. A Vercel build of this branch
# migrates the production database while production's code — which does not know the new column —
# keeps creating a contact for every new number that writes. Django 4.2 adds a NOT NULL column and
# drops its default again (it has no db_default), so those inserts would fail; the database keeps
# the default of `opted_out`. The two new tables are written by the new code only.
#
# `wahub_contacts` is written all day, and the new tables point at it, at the shadow replies, at
# the refine threads and at the users. Each of those needs a moment's lock, held until this
# migration commits; if something else holds a table, waiting behind it would queue every query on
# it. So the lock is asked for five seconds and no longer (as 0010 does): a build that cannot get it
# fails, changes nothing, and is simply run again.
#
# The new tables get row level security with no policy, owner only, as every table of this app
# has (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md). The questions kept in them carry no name and no
# phone (unknowns.scrub), and are still nobody's to read but the application's.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


RLS_TABLES = (
    'wahub_bot_unknown_topics',
    'wahub_bot_unknowns',
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
        ('wahub', '0010_waiting_notify_note_edit'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name='contact',
            name='opted_out',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "wahub_contacts" ALTER COLUMN "opted_out" SET DEFAULT \'{}\'::jsonb',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name='contactevent',
            name='kind',
            field=models.CharField(choices=[('created', 'נוצר'), ('followup_changed', 'סימון מעקב'), ('tags_changed', 'תגיות'), ('kogo_outcome_changed', 'מצב במערכת'), ('analyzed', 'סיכום אוטומטי'), ('handled_by_changed', 'מי עונה'), ('needs_human_changed', 'מבקש נציג'), ('note', 'הערה'), ('live_test', 'בדיקה חיה'), ('lead_stage_changed', 'מצב הליד'), ('lead_followup', 'חזרה של הבוט לליד'), ('lead_action', 'פעולה על הליד'), ('conversation_closed', 'השיחה נסגרה'), ('waiting_list', 'רשימת המתנה'), ('note_changed', 'שינוי בפתק'), ('opt_out', 'הסר מתפוצה')], max_length=30),
        ),
        migrations.CreateModel(
            name='BotUnknownTopic',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=120)),
                ('status', models.CharField(choices=[('open', 'פתוח'), ('taught', 'נלמד'), ('dismissed', 'לא רלוונטי')], default='open', max_length=10)),
                ('keywords', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('taught_at', models.DateTimeField(blank=True, null=True)),
                ('dismissed_at', models.DateTimeField(blank=True, null=True)),
                ('dismissed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('taught_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('taught_thread', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='wahub.refinethread')),
            ],
            options={
                'verbose_name': 'נושא שהבוט לא ידע',
                'verbose_name_plural': 'נושאים שהבוט לא ידע',
                'db_table': 'wahub_bot_unknown_topics',
                'ordering': ['-id'],
            },
        ),
        migrations.CreateModel(
            name='BotUnknown',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('question', models.TextField()),
                ('answer', models.TextField(blank=True)),
                ('hint', models.CharField(blank=True, max_length=300)),
                ('how', models.CharField(choices=[('model', 'הבוט סימן'), ('phrasing', 'הנוסח "בודקת מול המשרד"'), ('handoff', 'העברה לנציג כי חסר מידע')], max_length=10)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('contact', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='wahub.contact')),
                ('shadow', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='wahub.shadowreply')),
                ('topic', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='unknowns', to='wahub.botunknowntopic')),
            ],
            options={
                'verbose_name': 'שאלה שהבוט לא ידע',
                'verbose_name_plural': 'שאלות שהבוט לא ידע',
                'db_table': 'wahub_bot_unknowns',
                'ordering': ['-id'],
                'indexes': [models.Index(fields=['topic', '-id'], name='wahub_unknown_topic'), models.Index(fields=['created_at'], name='wahub_unknown_created')],
            },
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
