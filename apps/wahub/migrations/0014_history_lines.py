"""
The conversation from before the switch (the owner, 14.10.2026: "תתחבר ותעלה את השיחות האלה שיהיו פה
במערכת, שנדע אחורה"): the old bot's lines, the customers' and the office's, copied once from ManyChat.

A table of its own, apart from wahub_messages: every reading of a thread there goes by the row's id
(paging, the bot's context, "the last message"), and a line from last month copied today would read as
the newest. Row level security on, with no policy, like every table of the app.
"""
from django.db import migrations, models
import django.db.models.deletion


RLS_TABLES = ('wahub_history_lines',)


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
        ('wahub', '0013_settle_before_the_switch'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name='contact',
            name='history_copied_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='HistoryLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sent_at', models.DateTimeField()),
                ('direction', models.CharField(choices=[('in', 'נכנסת'), ('out', 'יוצאת')], max_length=3)),
                ('sender', models.CharField(choices=[('customer', 'לקוח'), ('bot', 'בוט'), ('office', 'משרד'), ('system', 'מערכת')], max_length=10)),
                ('text', models.TextField(blank=True)),
                ('message_type', models.CharField(choices=[('text', 'טקסט'), ('voice', 'הודעה קולית'), ('image', 'תמונה'), ('template', 'תבנית'), ('other', 'אחר')], default='text', max_length=10)),
                ('external_id', models.CharField(max_length=120)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('contact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='history_lines', to='wahub.contact')),
            ],
            options={
                'db_table': 'wahub_history_lines',
                'ordering': ['sent_at', 'id'],
                'indexes': [models.Index(fields=['contact', 'sent_at'], name='wahub_history_contact_at')],
            },
        ),
        migrations.AddConstraint(
            model_name='historyline',
            constraint=models.UniqueConstraint(fields=('contact', 'external_id'), name='wahub_history_line_once'),
        ),
        migrations.RunPython(enable_row_level_security, disable_row_level_security),
    ]
