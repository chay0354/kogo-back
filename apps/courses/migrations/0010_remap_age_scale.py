"""
Remap Course.min_age / Course.max_age from the old 1-18 scale (individual
preschool ages 1-6, then grades 7-18 mapped to א-י"ב) to the new 1-14 scale
(two preschool bands 1-2, then grades 3-14 mapped to א-י"ב).

Old ages 1-3 -> new 1 (3-4.5 band); old ages 4-6 -> new 2 (4.5-6 band).
Old grades 7-18 -> new 3-14 (same grade order, shifted down by 4).
"""
from django.db import migrations


def _remap(old_age):
    if old_age is None:
        return None
    if old_age <= 3:
        return 1
    if old_age <= 6:
        return 2
    return old_age - 4


def remap_forward(apps, schema_editor):
    Course = apps.get_model('courses', 'Course')
    for course in Course.objects.all().only('id', 'min_age', 'max_age'):
        new_min = _remap(course.min_age)
        new_max = _remap(course.max_age)
        if new_min != course.min_age or new_max != course.max_age:
            course.min_age = new_min
            course.max_age = new_max
            course.save(update_fields=['min_age', 'max_age'])


def _remap_back(new_age):
    if new_age is None:
        return None
    if new_age == 1:
        return 3
    if new_age == 2:
        return 6
    return new_age + 4


def remap_backward(apps, schema_editor):
    Course = apps.get_model('courses', 'Course')
    for course in Course.objects.all().only('id', 'min_age', 'max_age'):
        old_min = _remap_back(course.min_age)
        old_max = _remap_back(course.max_age)
        if old_min != course.min_age or old_max != course.max_age:
            course.min_age = old_min
            course.max_age = old_max
            course.save(update_fields=['min_age', 'max_age'])


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0009_course_external_link'),
    ]

    operations = [
        migrations.RunPython(remap_forward, remap_backward),
    ]
