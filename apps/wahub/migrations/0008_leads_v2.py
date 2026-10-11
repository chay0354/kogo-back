# Leads, version 2 (docs/WAHUB-CONTRACT-LEADS-V2.md): what the conversation says about a
# lead (`lead_ai`, filled by the summary pass), who makes the next move (`lead_owner`), the
# branch / kind of class / day to come back as columns to filter by, when a registration link
# last went out, what a person decided on the leads screen (`lead_manual`), where the bot's own
# follow-ups stand (`lead_bot`), the version of the rules that last looked at the contact — and
# the table of those follow-ups. Additions only: nothing is backfilled, renamed or dropped. The
# two AlterFields change `choices` alone and touch nothing in the database; a contact whose
# stage was kept as "not_relevant" keeps that value until the cron looks at it again (the rules'
# version sends every contact through one more look, a batch at a time).
#
# Safe for the code that is already running, the way 0004–0007 are. A Vercel build of this
# branch migrates the production database while production's code — which knows none of these
# columns — keeps creating a contact for every new number that writes. Django 4.2 adds a NOT
# NULL column and drops its default again (it has no db_default), so those inserts would fail;
# the database keeps the default of each of the six columns that do not allow NULL. The other
# three allow it. The new table is written by the new code only.
#
# The new table gets row level security with no policy, owner only, as every table of this
# app has (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).

from django.db import migrations, models
import django.db.models.deletion


RLS_TABLES = (
    'wahub_lead_followups',
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


def _keep_default(column: str, default: str):
    return migrations.RunSQL(
        sql=f'ALTER TABLE "wahub_contacts" ALTER COLUMN "{column}" SET DEFAULT {default}',
        reverse_sql=migrations.RunSQL.noop,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0007_bot_evals'),
    ]

    operations = [
        migrations.AddField(
            model_name='contact',
            name='lead_ai',
            field=models.JSONField(blank=True, default=dict),
        ),
        _keep_default('lead_ai', "'{}'::jsonb"),
        migrations.AddField(
            model_name='contact',
            name='lead_bot',
            field=models.JSONField(blank=True, default=dict),
        ),
        _keep_default('lead_bot', "'{}'::jsonb"),
        migrations.AddField(
            model_name='contact',
            name='lead_branch_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contact',
            name='lead_come_back_on',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contact',
            name='lead_course_type',
            field=models.CharField(blank=True, max_length=100),
        ),
        _keep_default('lead_course_type', "''"),
        migrations.AddField(
            model_name='contact',
            name='lead_link_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contact',
            name='lead_manual',
            field=models.JSONField(blank=True, default=dict),
        ),
        _keep_default('lead_manual', "'{}'::jsonb"),
        migrations.AddField(
            model_name='contact',
            name='lead_owner',
            field=models.CharField(blank=True, choices=[('bot', 'הבוט מטפל'), ('office', 'מחכה למשרד')], max_length=10),
        ),
        _keep_default('lead_owner', "''"),
        migrations.AddField(
            model_name='contact',
            name='lead_version',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        _keep_default('lead_version', '0'),
        migrations.AlterField(
            model_name='contact',
            name='lead_stage',
            field=models.CharField(blank=True, choices=[('signup_stuck', 'נתקע בהרשמה'), ('needs_reply', 'שאל ולא קיבל מענה'), ('signup_failed', 'ניסה להירשם והחיוב נכשל'), ('signup_started', 'התחיל הרשמה ולא סיים'), ('link_sent', 'קיבל קישור ועוד לא נרשם'), ('trial_no_show', 'נרשם לניסיון ולא הגיע'), ('trial_done', 'עשה ניסיון ולא נרשם'), ('callback_due', 'הגיע הזמן לחזור אליו'), ('answered_silent', 'קיבל תשובה ולא נרשם'), ('will_check', 'אמר שיבדוק ויחזור'), ('trial_booked', 'רשום לשיעור ניסיון'), ('waiting_date', 'מחכה למועד'), ('nothing_to_offer', 'אין כרגע מה להציע'), ('in_conversation', 'בשיחה'), ('registered', 'נרשם'), ('customer', 'לקוח קיים'), ('not_interested', 'לא מעוניין'), ('deleted', 'נמחק'), ('cold', 'רדום')], max_length=20),
        ),
        migrations.AlterField(
            model_name='contactevent',
            name='kind',
            field=models.CharField(choices=[('created', 'נוצר'), ('followup_changed', 'סימון מעקב'), ('tags_changed', 'תגיות'), ('kogo_outcome_changed', 'מצב במערכת'), ('analyzed', 'סיכום אוטומטי'), ('handled_by_changed', 'מי עונה'), ('needs_human_changed', 'מבקש נציג'), ('note', 'הערה'), ('live_test', 'בדיקה חיה'), ('lead_stage_changed', 'מצב הליד'), ('lead_followup', 'חזרה של הבוט לליד'), ('lead_action', 'פעולה על הליד')], max_length=30),
        ),
        migrations.CreateModel(
            name='LeadFollowUp',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('link_reminder', 'קיבל קישור ועוד לא נרשם'), ('will_check', 'אמר שיבדוק ויחזור'), ('trial_feedback', 'אחרי שיעור ניסיון'), ('trial_no_show', 'לא הגיע לשיעור ניסיון'), ('date_arrived', 'הגיע המועד שדובר עליו'), ('opening', 'פנה ולא כתב מה הוא מחפש'), ('dormant', 'ליד רדום — נפתחה הרשמה')], max_length=20)),
                ('step', models.PositiveSmallIntegerField(default=1)),
                ('episode', models.CharField(blank=True, max_length=160)),
                ('due_at', models.DateTimeField()),
                ('next_look_at', models.DateTimeField()),
                ('status', models.CharField(choices=[('planned', 'מתוכנן'), ('due', 'הגיע הזמן — לא נשלח'), ('sent', 'נשלח'), ('skipped', 'בוטל'), ('failed', 'נכשל')], default='planned', max_length=10)),
                ('text', models.TextField(blank=True)),
                ('template_key', models.CharField(blank=True, max_length=40)),
                ('why', models.CharField(blank=True, max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('acted_at', models.DateTimeField(blank=True, null=True)),
                ('outcome_note', models.CharField(blank=True, max_length=300)),
                ('claimed_at', models.DateTimeField(blank=True, null=True)),
                ('contact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lead_followups', to='wahub.contact')),
                ('message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='wahub.message')),
            ],
            options={
                'verbose_name': 'חזרה לליד',
                'verbose_name_plural': 'חזרות ללידים',
                'db_table': 'wahub_lead_followups',
                'ordering': ['-id'],
                'indexes': [models.Index(fields=['contact', '-id'], name='wahub_nudge_contact'), models.Index(condition=models.Q(('status__in', ('planned', 'due'))), fields=['next_look_at'], name='wahub_nudge_open')],
            },
        ),
        migrations.AddConstraint(
            model_name='leadfollowup',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ('planned', 'due'))), fields=('contact',), name='wahub_nudge_one_open'),
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
