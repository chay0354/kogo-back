from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0012_course_must_attend_all_lessons'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='trial_lesson_is_paid',
            field=models.BooleanField(
                default=False,
                help_text="כאשר מופעל, הרשמה לשיעור ניסיון דרך הווידג'ט תחייב את המחיר שמוגדר.",
                verbose_name='שיעור ניסיון בתשלום',
            ),
        ),
        migrations.AddField(
            model_name='course',
            name='trial_lesson_price',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=10,
                null=True,
                verbose_name='מחיר שיעור ניסיון (₪)',
            ),
        ),
    ]
