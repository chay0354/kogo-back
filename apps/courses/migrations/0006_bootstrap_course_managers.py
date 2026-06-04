"""
Bootstrap manager scoping: assign every existing course to every existing
manager so no manager is locked out the moment scoping goes live. Managers can
then trim assignments per course from the course dialog.
"""
from django.db import migrations


def assign_existing(apps, schema_editor):
    Course = apps.get_model('courses', 'Course')
    UserProfile = apps.get_model('core', 'UserProfile')

    manager_user_ids = list(
        UserProfile.objects.filter(role='manager').values_list('user_id', flat=True)
    )
    if not manager_user_ids:
        return

    for course in Course.objects.all().only('id'):
        course.managers.add(*manager_user_ids)


def unassign(apps, schema_editor):
    Course = apps.get_model('courses', 'Course')
    for course in Course.objects.all().only('id'):
        course.managers.clear()


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0005_course_managers'),
    ]

    operations = [
        migrations.RunPython(assign_existing, unassign),
    ]
