# The live test from the owner's own WhatsApp (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב):
# the mark on a contact the NEW bot answers for real, the claim an answer in
# progress holds, ManyChat's raw body kept on such a contact's messages, and the
# journal kind that records the test being turned on and off. Nothing is
# backfilled and nothing is dropped.
#
# Safe for the code that is already running. A Vercel build of this branch
# migrates the production database while production's code — which does not know
# `live_test` — keeps creating a contact for every new number that writes.
# Django 4.2 adds the column NOT NULL and drops its default again (it has no
# db_default), so those inserts would fail; the database keeps the `false`
# default (as customers/0023 does). The other two columns allow NULL.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0003_message_transcript_new_bot'),
    ]

    operations = [
        migrations.AddField(
            model_name='contact',
            name='live_test',
            field=models.BooleanField(default=False),
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "wahub_contacts" ALTER COLUMN "live_test" SET DEFAULT false',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name='contact',
            name='live_test_busy_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='message',
            name='raw',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='contactevent',
            name='kind',
            field=models.CharField(choices=[
                ('created', 'נוצר'), ('followup_changed', 'סימון מעקב'), ('tags_changed', 'תגיות'),
                ('kogo_outcome_changed', 'מצב במערכת'), ('analyzed', 'סיכום אוטומטי'), ('handled_by_changed', 'מי עונה'),
                ('needs_human_changed', 'מבקש נציג'), ('note', 'הערה'), ('live_test', 'בדיקה חיה'),
            ], max_length=30),
        ),
    ]
