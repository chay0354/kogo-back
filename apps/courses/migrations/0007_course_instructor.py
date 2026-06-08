from django.db import migrations, models
import django.db.models.deletion


def bootstrap_course_instructors(apps, schema_editor):
    Course = apps.get_model('courses', 'Course')
    Lesson = apps.get_model('courses', 'Lesson')

    for course in Course.objects.all():
        lesson = (
            Lesson.objects.filter(course_id=course.id, instructor_id__isnull=False)
            .order_by('day_of_week', 'start_time')
            .first()
        )
        if not lesson:
            continue
        course.instructor_id = lesson.instructor_id
        course.instructor_salary_override = lesson.instructor_salary_override
        course.save(update_fields=['instructor_id', 'instructor_salary_override'])


class Migration(migrations.Migration):

    dependencies = [
        ('instructors', '0001_initial'),
        ('courses', '0006_bootstrap_course_managers'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='instructor',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='courses',
                to='instructors.instructor',
                verbose_name='מדריך',
            ),
        ),
        migrations.AddField(
            model_name='course',
            name='instructor_salary_override',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=10,
                null=True,
                verbose_name='שכר מדריך מותאם',
            ),
        ),
        migrations.RunPython(bootstrap_course_instructors, migrations.RunPython.noop),
    ]
