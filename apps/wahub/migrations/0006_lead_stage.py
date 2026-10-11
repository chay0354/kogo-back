# The lead stage (docs/WAHUB-CONTRACT-LEAD-STAGES.md): one stage per contact, the
# sentence of why and the sentence of what to do, when it was last worked out and
# when the clock asks for another look (apps/wahub/lead_stage.py); the trial behind
# a "trial" answer of the matching, as facts (kogo_trial); and the journal kind that
# records a change of stage. Nothing is backfilled here and nothing is dropped: every
# contact starts with no stage, and the cron fills them in a batch at a time.
#
# Safe for the code that is already running, the way 0004 and 0005 are. A Vercel build of this
# branch migrates the production database while production's code — which knows none
# of these columns — keeps creating a contact for every new number that writes.
# Django 4.2 adds a NOT NULL column and drops its default again (it has no
# db_default), so those inserts would fail; the database keeps the default of each of
# the five columns that do not allow NULL. The other three allow it.

from django.db import migrations, models


def _keep_default(column: str, default: str):
    return migrations.RunSQL(
        sql=f'ALTER TABLE "wahub_contacts" ALTER COLUMN "{column}" SET DEFAULT {default}',
        reverse_sql=migrations.RunSQL.noop,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0005_refine'),
    ]

    operations = [
        migrations.AddField(
            model_name='contact',
            name='kogo_trial',
            field=models.JSONField(blank=True, default=dict),
        ),
        _keep_default('kogo_trial', "'{}'::jsonb"),
        migrations.AddField(
            model_name='contact',
            name='lead_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contact',
            name='lead_human_touched',
            field=models.BooleanField(default=False),
        ),
        _keep_default('lead_human_touched', 'false'),
        migrations.AddField(
            model_name='contact',
            name='lead_next_step',
            field=models.CharField(blank=True, max_length=200),
        ),
        _keep_default('lead_next_step', "''"),
        migrations.AddField(
            model_name='contact',
            name='lead_reason',
            field=models.CharField(blank=True, max_length=300),
        ),
        _keep_default('lead_reason', "''"),
        migrations.AddField(
            model_name='contact',
            name='lead_recheck_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contact',
            name='lead_stage',
            field=models.CharField(blank=True, choices=[
                ('needs_reply', 'שאל ולא קיבל תשובה'), ('signup_failed', 'ניסה להירשם והחיוב נכשל'),
                ('signup_started', 'התחיל הרשמה ולא סיים'), ('trial_no_show', 'נרשם לניסיון ולא הגיע'),
                ('trial_done', 'עשה ניסיון ולא נרשם'), ('callback_due', 'הגיע הזמן לחזור אליו'),
                ('answered_silent', 'קיבל תשובה ולא נרשם'), ('trial_booked', 'רשום לשיעור ניסיון'),
                ('waiting_date', 'ממתין לתאריך'), ('in_conversation', 'בשיחה'), ('registered', 'נרשם'),
                ('customer', 'לקוח קיים'), ('not_relevant', 'לא רלוונטי'), ('cold', 'ליד ישן'),
            ], max_length=20),
        ),
        _keep_default('lead_stage', "''"),
        migrations.AddField(
            model_name='contact',
            name='lead_stage_since',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='contactevent',
            name='kind',
            field=models.CharField(choices=[
                ('created', 'נוצר'), ('followup_changed', 'סימון מעקב'), ('tags_changed', 'תגיות'),
                ('kogo_outcome_changed', 'מצב במערכת'), ('analyzed', 'סיכום אוטומטי'), ('handled_by_changed', 'מי עונה'),
                ('needs_human_changed', 'מבקש נציג'), ('note', 'הערה'), ('live_test', 'בדיקה חיה'),
                ('lead_stage_changed', 'מצב הליד'),
            ], max_length=30),
        ),
        migrations.AddIndex(
            model_name='contact',
            index=models.Index(fields=['lead_stage', 'lead_stage_since'], name='wahub_contact_lead_stage'),
        ),
        migrations.AddIndex(
            model_name='contact',
            index=models.Index(condition=models.Q(('lead_recheck_at__isnull', False)), fields=['lead_recheck_at'], name='wahub_contact_lead_recheck'),
        ),
    ]
