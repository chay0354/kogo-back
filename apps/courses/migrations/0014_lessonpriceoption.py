import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0013_course_trial_lesson_paid'),
    ]

    operations = [
        migrations.CreateModel(
            name='LessonPriceOption',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('display_title', models.CharField(max_length=200, verbose_name="כותרת בווידג'ט")),
                ('monthly_price', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='מחיר חודשי')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='סדר תצוגה')),
                ('is_active', models.BooleanField(default=True, verbose_name='פעיל')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('lesson', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='price_options', to='courses.lesson', verbose_name='שיעור')),
            ],
            options={
                'verbose_name': 'מחיר נוסף לשיעור',
                'verbose_name_plural': 'מחירים נוספים לשיעור',
                'db_table': 'lesson_price_options',
                'ordering': ['sort_order', 'display_title'],
            },
        ),
    ]
