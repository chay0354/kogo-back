# Telling a customer that a seat came free, and rewriting or deleting a note
# (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ז): four columns on the waiting list — when the customer
# was told, by whom and how, and the mark of a telling that is being sent this moment — and two on
# the contact's journal — when a note was rewritten and by whom. Additions only: nothing is backfilled, renamed or dropped. The AlterField changes `choices`
# alone (a new kind of journal line, "שינוי בפתק") and touches nothing in the database.
#
# Safe for the code that is already running, the way 0004–0009 are. A Vercel build of this branch
# migrates the production database while production's code — which does not know the new columns —
# keeps writing a journal line for every message and a waiting-list row whenever the bot or the
# office adds one. Five of the columns are nullable, so those inserts simply leave them empty.
# `notified_how` is NOT NULL: Django 4.2 adds it with a default and drops the default again (it has
# no db_default), so the database keeps the default itself.
#
# Both tables are written all day. Each ALTER needs a moment's exclusive lock, held until this
# migration commits; if something else holds the table, waiting behind it would queue every query
# on the journal. So the lock is asked for five seconds and no longer (as apps/rentals does): a
# build that cannot get it fails, changes nothing, and is simply run again. For the same reason
# `edited_by` gets no index — building one would read the whole journal under that lock.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('wahub', '0009_waiting_list_future_leads'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name='contactevent',
            name='edited_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contactevent',
            name='edited_by',
            field=models.ForeignKey(blank=True, db_index=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='waitinglistentry',
            name='notified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='waitinglistentry',
            name='notified_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='waitinglistentry',
            name='notified_how',
            field=models.CharField(blank=True, choices=[('text', 'הודעה רגילה'), ('template', 'תבנית')], default='', max_length=10),
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "wahub_waiting_list" ALTER COLUMN "notified_how" SET DEFAULT \'\'',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name='waitinglistentry',
            name='notify_claimed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='contactevent',
            name='kind',
            field=models.CharField(choices=[('created', 'נוצר'), ('followup_changed', 'סימון מעקב'), ('tags_changed', 'תגיות'), ('kogo_outcome_changed', 'מצב במערכת'), ('analyzed', 'סיכום אוטומטי'), ('handled_by_changed', 'מי עונה'), ('needs_human_changed', 'מבקש נציג'), ('note', 'הערה'), ('live_test', 'בדיקה חיה'), ('lead_stage_changed', 'מצב הליד'), ('lead_followup', 'חזרה של הבוט לליד'), ('lead_action', 'פעולה על הליד'), ('conversation_closed', 'השיחה נסגרה'), ('waiting_list', 'רשימת המתנה'), ('note_changed', 'שינוי בפתק')], max_length=30),
        ),
    ]
