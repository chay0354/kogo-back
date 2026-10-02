from django.db import migrations

# The owner, 2.10.2026: the Em HaMoshavot studio is in the Sapir mall, on the
# first floor. Written only where nothing was written yet.
WORDS = 'קניון ספיר, קומה 1.'


def fill(apps, schema_editor):
    Branch = apps.get_model('core', 'Branch')
    for branch in Branch.objects.filter(name__contains='אם המושבות'):
        if (branch.arrival_directions or '').strip():
            continue
        branch.arrival_directions = WORDS
        branch.save(update_fields=['arrival_directions'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_trial_calendar_details'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
