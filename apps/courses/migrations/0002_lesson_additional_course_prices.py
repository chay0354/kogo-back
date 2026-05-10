from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='lesson',
            name='additional_course_prices',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "List of {course_index, price} entries used when a child is concurrently "
                    "enrolled in N other courses. course_index is 1-based: 2 = student's 2nd "
                    "course, 3 = 3rd, ... Used by get_lesson_price_for_course_index to pick a "
                    "discounted/different price per tier."
                ),
                verbose_name='מחירים מדורגים לרישום מקביל',
            ),
        ),
    ]
