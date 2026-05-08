from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='lesson',
            name='max_students',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Optional cap on enrollments for this lesson. Defaults to room capacity when null.',
                null=True,
                verbose_name='מקסימום תלמידים',
            ),
        ),
    ]
