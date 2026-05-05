# Snapshot models live in core only; instructors 0001_initial never created them here.
# Keep this migration as a no-op so the graph stays linear on fresh installs.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('instructors', '0001_initial'),
        ('core', '0003_merge_20260505_core'),
    ]

    operations = []
