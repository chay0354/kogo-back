# The waiting list of a lesson that is full, the future leads and the notes
# (docs/WAHUB-CONTRACT-FUTURE-LEADS.md): one new table — who waits for which lesson — and one
# column on the contact, the copy of its place on a waiting list that the lead stage and the
# lists read (`lead_waiting`). Additions only: nothing is backfilled, renamed or dropped. The
# two AlterFields change `choices` alone (a new lead stage, two new kinds of journal line) and
# touch nothing in the database. The future leads and the closing of a conversation need no
# column: they live on the marks a person already fills in (`followup_*`).
#
# Safe for the code that is already running, the way 0004–0008 are. A Vercel build of this
# branch migrates the production database while production's code — which does not know the
# new column — keeps creating a contact for every new number that writes. Django 4.2 adds a NOT
# NULL column and drops its default again (it has no db_default), so those inserts would fail;
# the database keeps the default of `lead_waiting`. The new table is written by the new code only.
#
# The new table gets row level security with no policy, owner only, as every table of this
# app has (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


RLS_TABLES = (
    'wahub_waiting_list',
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
        ('wahub', '0008_leads_v2'),
    ]

    operations = [
        migrations.AddField(
            model_name='contact',
            name='lead_waiting',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "wahub_contacts" ALTER COLUMN "lead_waiting" SET DEFAULT \'{}\'::jsonb',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name='contact',
            name='lead_stage',
            field=models.CharField(blank=True, choices=[('signup_stuck', 'נתקע בהרשמה'), ('needs_reply', 'שאל ולא קיבל מענה'), ('signup_failed', 'ניסה להירשם והחיוב נכשל'), ('signup_started', 'התחיל הרשמה ולא סיים'), ('link_sent', 'קיבל קישור ועוד לא נרשם'), ('trial_no_show', 'נרשם לניסיון ולא הגיע'), ('trial_done', 'עשה ניסיון ולא נרשם'), ('callback_due', 'הגיע הזמן לחזור אליו'), ('answered_silent', 'קיבל תשובה ולא נרשם'), ('will_check', 'אמר שיבדוק ויחזור'), ('trial_booked', 'רשום לשיעור ניסיון'), ('waiting_date', 'מחכה למועד'), ('waiting_list', 'ברשימת המתנה'), ('nothing_to_offer', 'אין כרגע מה להציע'), ('in_conversation', 'בשיחה'), ('registered', 'נרשם'), ('customer', 'לקוח קיים'), ('not_interested', 'לא מעוניין'), ('deleted', 'נמחק'), ('cold', 'רדום')], max_length=20),
        ),
        migrations.AlterField(
            model_name='contactevent',
            name='kind',
            field=models.CharField(choices=[('created', 'נוצר'), ('followup_changed', 'סימון מעקב'), ('tags_changed', 'תגיות'), ('kogo_outcome_changed', 'מצב במערכת'), ('analyzed', 'סיכום אוטומטי'), ('handled_by_changed', 'מי עונה'), ('needs_human_changed', 'מבקש נציג'), ('note', 'הערה'), ('live_test', 'בדיקה חיה'), ('lead_stage_changed', 'מצב הליד'), ('lead_followup', 'חזרה של הבוט לליד'), ('lead_action', 'פעולה על הליד'), ('conversation_closed', 'השיחה נסגרה'), ('waiting_list', 'רשימת המתנה')], max_length=30),
        ),
        migrations.CreateModel(
            name='WaitingListEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('lesson_id', models.UUIDField()),
                ('course_id', models.UUIDField(blank=True, null=True)),
                ('branch_id', models.UUIDField(blank=True, null=True)),
                ('course_name', models.CharField(blank=True, max_length=200)),
                ('branch_name', models.CharField(blank=True, max_length=200)),
                ('day_of_week', models.SmallIntegerField(blank=True, null=True)),
                ('start_time', models.TimeField(blank=True, null=True)),
                ('end_time', models.TimeField(blank=True, null=True)),
                ('child', models.CharField(blank=True, max_length=200)),
                ('status', models.CharField(choices=[('waiting', 'ממתין'), ('freed', 'התפנה מקום'), ('handled', 'טופל'), ('removed', 'הוסר')], default='waiting', max_length=10)),
                ('added_by_kind', models.CharField(choices=[('bot', 'הבוט'), ('person', 'נציג')], default='bot', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('freed_at', models.DateTimeField(blank=True, null=True)),
                ('checked_at', models.DateTimeField(blank=True, null=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('closed_reason', models.CharField(blank=True, max_length=200)),
                ('added_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('contact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='waiting_list', to='wahub.contact')),
            ],
            options={
                'verbose_name': 'רשומה ברשימת המתנה',
                'verbose_name_plural': 'רשימת המתנה',
                'db_table': 'wahub_waiting_list',
                'ordering': ['id'],
                'indexes': [models.Index(fields=['contact', '-id'], name='wahub_waiting_contact'), models.Index(condition=models.Q(('status__in', ('waiting', 'freed'))), fields=['lesson_id', 'id'], name='wahub_waiting_active')],
            },
        ),
        migrations.AddConstraint(
            model_name='waitinglistentry',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ('waiting', 'freed'))), fields=('contact', 'lesson_id'), name='wahub_waiting_one_active'),
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
