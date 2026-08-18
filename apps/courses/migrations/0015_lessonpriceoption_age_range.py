from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0014_lessonpriceoption'),
    ]

    operations = [
        migrations.AddField(
            model_name='lessonpriceoption',
            name='min_age',
            field=models.PositiveIntegerField(
                blank=True,
                help_text="ריק = אותה קבוצת גיל כמו החוג. כשמוגדר, השורה תופיע גם כשבוחרים גיל זה בווידג'ט.",
                null=True,
                verbose_name="גיל מינימום בווידג'ט",
            ),
        ),
        migrations.AddField(
            model_name='lessonpriceoption',
            name='max_age',
            field=models.PositiveIntegerField(
                blank=True,
                help_text="ריק = אותה קבוצת גיל כמו החוג.",
                null=True,
                verbose_name="גיל מקסימום בווידג'ט",
            ),
        ),
    ]
