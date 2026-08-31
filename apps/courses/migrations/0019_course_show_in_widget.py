from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0018_lessonbundle_age_range'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='show_in_widget',
            field=models.BooleanField(
                default=True,
                help_text="כבוי = החוג נשאר ב-CRM אבל לא מופיע בהרשמה הציבורית.",
                verbose_name="מוצג בווידג'ט",
            ),
        ),
    ]
