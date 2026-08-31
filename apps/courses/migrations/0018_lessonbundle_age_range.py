from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0017_course_immediate_standing_order'),
    ]

    operations = [
        migrations.AddField(
            model_name='lessonbundle',
            name='min_age',
            field=models.PositiveIntegerField(
                blank=True,
                help_text="ריק = אותה קבוצת גיל כמו החוג. כשמוגדר, המסלול יופיע גם כשבוחרים גיל זה בווידג'ט.",
                null=True,
                verbose_name="גיל מינימום בווידג'ט",
            ),
        ),
        migrations.AddField(
            model_name='lessonbundle',
            name='max_age',
            field=models.PositiveIntegerField(
                blank=True,
                help_text="ריק = אותה קבוצת גיל כמו החוג.",
                null=True,
                verbose_name="גיל מקסימום בווידג'ט",
            ),
        ),
    ]
