# A real conversation with a demo contact (docs/WAHUB-CONTRACT-REAL-DEMO.md):
# the transcript of a recording the owner sent, and the mark on the NEW bot's
# answers written into the demo conversation. Two nullable-by-default columns on
# an existing table; nothing is backfilled and nothing is dropped.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0002_knowledge_shadow_review'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='transcript',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='message',
            name='new_bot',
            field=models.BooleanField(default=False),
        ),
    ]
