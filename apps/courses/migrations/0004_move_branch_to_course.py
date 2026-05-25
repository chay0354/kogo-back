from django.db import migrations, models
import django.db.models.deletion


def populate_course_branch(apps, schema_editor):
    """Populate Course.branch from the first lesson's branch for courses where branch is null."""
    Course = apps.get_model('courses', 'Course')
    Lesson = apps.get_model('courses', 'Lesson')

    for course in Course.objects.filter(branch__isnull=True):
        first_lesson = Lesson.objects.filter(course=course).first()
        if first_lesson:
            course.branch_id = first_lesson.branch_id
            course.save(update_fields=['branch_id'])


def reverse_populate_lesson_branch(apps, schema_editor):
    """Restore Lesson.branch from Course.branch for each lesson (reverse migration)."""
    Lesson = apps.get_model('courses', 'Lesson')

    for lesson in Lesson.objects.all():
        if lesson.course_id:
            lesson.branch_id = lesson.course.branch_id
            lesson.save(update_fields=['branch_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0003_lesson_lessons_branch__097a4a_idx_and_more'),
    ]

    operations = [
        # Step 1: populate Course.branch from Lesson.branch for courses where it is null
        migrations.RunPython(
            populate_course_branch,
            reverse_code=migrations.RunPython.noop,
        ),

        # Step 2: make Course.branch non-nullable
        migrations.AlterField(
            model_name='course',
            name='branch',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='courses',
                to='core.branch',
                verbose_name='סניף',
            ),
        ),

        # Step 3-4: drop branch-based indexes from Lesson
        migrations.RemoveIndex(
            model_name='lesson',
            name='lessons_branch__097a4a_idx',
        ),
        migrations.RemoveIndex(
            model_name='lesson',
            name='lessons_branch__07b2f9_idx',
        ),

        # Step 5: forward noop; reverse populates lesson.branch from course.branch
        # before Django's auto-reverse re-adds the nullable column
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=reverse_populate_lesson_branch,
        ),

        # Step 6: drop Lesson.branch (reverse auto-adds it back as nullable)
        migrations.RemoveField(
            model_name='lesson',
            name='branch',
        ),
    ]
