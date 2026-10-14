"""
A contact the system opens by itself for a child who came to a lesson without being registered, whose phone
the instructor wrote down (apps/wahub/walk_ins.py): one more value of `source`, "הגיע לשיעור בלי להירשם".
A choice only — the column is not touched (sqlmigrate: no-op).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0014_history_lines'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contact',
            name='source',
            field=models.CharField(choices=[('whatsapp', 'וואטסאפ'), ('ad', 'מודעה'), ('broadcast_reply', 'תשובה לתפוצה'), ('kogo_trial', 'נרשם לניסיון במערכת'), ('kogo_signup', 'נרשם במערכת'), ('import', 'ייבוא'), ('manual', 'הוסף ידנית'), ('demo', 'דמו'), ('kogo_walk_in', 'הגיע לשיעור בלי להירשם')], default='whatsapp', max_length=20),
        ),
    ]
