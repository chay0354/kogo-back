"""Card links: a short public token, and the twice-a-week track a link bills.

Both columns are nullable so the migration is expand-only. `vercel_build.py`
runs `migrate` at build time, while the previous deployment is still answering
requests; that code inserts card links without these columns, and a NOT NULL
column without a database default would make every one of those inserts fail.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0011_lessonbundle'),
        ('payment_links', '0002_card_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='cardlink',
            name='token',
            field=models.CharField(blank=True, max_length=32, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='cardlink',
            name='bundle',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='card_links',
                to='courses.lessonbundle',
            ),
        ),
    ]
